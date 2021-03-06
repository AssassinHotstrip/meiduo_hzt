import time

from django.utils import timezone
from rest_framework import serializers
from decimal import Decimal
from django_redis import get_redis_connection
from django.db import transaction

from goods.models import SKU
from .models import OrderInfo, OrderGoods


class CommitOrderSerializer(serializers.ModelSerializer):
    """下单数据序列化器"""

    class Meta:
        model = OrderInfo
        fields = ['order_id', 'address', 'pay_method']
        read_only_fields = ['order_id']  # 只能输出,只做序列化
        extra_kwargs = {
            'address': {
                'write_only': True,  # 只能写入,只做反序列化
                'required': True,  # 必须传入
            },
            'pay_method': {
                'write_only': True,
                'required': True
            }
        }

    def create(self, validated_data):
        """保存订单信息-- 订单商品 --- SKU中的销量和库存和SPU中销量"""
        # 获取当前保存订单时需要的信息
        # 获取当前下单的用户
        user = self.context['request'].user
        # 生成订单编号 20181205105400000000001
        order_id = timezone.now().strftime('%Y%m%d%H%M%S') + ('%09d' % user.id)
        # 获取用户收货地址
        address = validated_data.get('address')
        # 获取用户付款方式
        pay_method = validated_data.get('pay_method')

        # 订单状态
        # status = '待支付' if 用户选择的时支付宝支付 else '待发货'
        status = (OrderInfo.ORDER_STATUS_ENUM['UNPAID'] if
                  pay_method == OrderInfo.PAY_METHODS_ENUM['ALIPAY'] else
                  OrderInfo.ORDER_STATUS_ENUM['UNSEND'])

        with transaction.atomic():  # 开启一个明显的事务

            # 创建事务保存点
            save_point = transaction.savepoint()
            try:
                # 保存订单基本信息 OrderInfo（一）
                order = OrderInfo.objects.create(
                    order_id=order_id,
                    user=user,
                    address=address,
                    total_count=0,
                    total_amount=Decimal('0.00'),
                    freight=Decimal('10.00'),
                    pay_method=pay_method,
                    status=status
                )

                # 从redis读取购物车中被勾选的商品信息
                # 获取redis的连接对象
                redis_conn = get_redis_connection('carts')

                # 获取购物车中的所有商品(有可能包含未勾选的)
                # {b'1': b'1', b'16': b'2'}  {sku_id: 数量}
                redis_cart_dict = redis_conn.hgetall('cart_%s' % user.id)

                # 获取购物车中所有勾选商品的sku_id
                # {b'1'}   # {勾选的商品sku_id}
                redis_selected = redis_conn.smembers('selected_%s' % user.id)
                carts = {}  # 用来保存所有勾选商品的id及数量{1: 1}  {勾选商品id: 购买数量}
                for sku_id in redis_selected:
                    carts[int(sku_id)] = int(redis_cart_dict[sku_id])

                # skus = SKU.objects.filter(id__in=carts.keys())  # 此处不要这样一下全取出,会有缓存问题,对于后续的并发及事务处理时,可能有干扰
                # 遍历购物车中被勾选的商品信息
                for sku_id in carts:

                    while True:
                        # 获取sku对象
                        sku = SKU.objects.get(id=sku_id)

                        # 获取要购买商品的原始销量和库存
                        origin_stock = sku.stock
                        origin_sales = sku.sales

                        # 获取当前商品购买量
                        sku_count = carts[sku_id]
                        # 判断库存 
                        if sku_count > origin_stock:
                            # transaction.rollback(save_point)  # 回到事务开启的地方
                            # transaction.savepoint_rollback(save_point)  # 回滚到指定的保存点
                            raise serializers.ValidationError('库存不足')

                        time.sleep(5)  # 延迟只会为了放大并发问题

                        # 减少库存，增加销量 SKU 
                        # sku.stock -= sku_count  # sku.stock = sku.stock - sku_count
                        # sku.sales += sku_count  # sku.sales = sku.sales + sku_count
                        # sku.save()

                        new_stock = origin_stock - sku_count
                        new_sales = origin_sales + sku_count
                        # 乐观锁(执行时括号一中条件任成立,则将update括号二)
                        result = SKU.objects.filter(id=sku_id, stock=origin_stock).update(stock=new_stock, sales=new_sales)
                        if result == 0:  # 如果返回值为0说明用户下单失败
                            # 1.库存充足
                            # 2.没有其它用户和你抢同一个商品
                            continue

                        # 修改SPU销量  sku.goods  通过外键获取SPU
                        spu = sku.goods
                        spu.sales += sku_count
                        spu.save()

                        # 保存订单商品信息 OrderGoods（多）
                        OrderGoods.objects.create(
                            order=order,  # order_id = order.id
                            sku=sku,
                            count=sku_count,
                            price=sku.price,
                        )

                        # 累加计算总数量和总价
                        order.total_count += sku_count
                        order.total_amount += (sku.price * sku_count)

                        # 下单已经成功跳出while True 死循环
                        break

                # 最后加入邮费和保存订单信息
                order.total_amount += order.freight
                order.save()


            except Exception:
                # 暴力回滚,无论中间出现什么问题都回滚
                # transaction.rollback(save_point)
                transaction.savepoint_rollback(save_point)
                raise

            # 提交事件
            transaction.savepoint_commit(save_point)

        # 清除购物车中已结算的商品
        # 删除购物车中哈希字典中已经购买过的商品
        pl = redis_conn.pipeline()
        pl.hdel('cart_%s' % user.id, *redis_selected)
        pl.srem('selected_%s' % user.id, *redis_selected)

        # 执行管道
        pl.execute()

        # 返回订单模型对象
        return order


class CartSKUSerializer(serializers.ModelSerializer):
    """
    购物车商品数据序列化器
    """
    count = serializers.IntegerField(label='数量')

    class Meta:
        model = SKU
        fields = ('id', 'name', 'default_image_url', 'price', 'count')


class OrderSettlementSerializer(serializers.Serializer):
    """
    订单结算数据序列化器
    """
    freight = serializers.DecimalField(label='运费', max_digits=10, decimal_places=2)
    skus = CartSKUSerializer(many=True)
