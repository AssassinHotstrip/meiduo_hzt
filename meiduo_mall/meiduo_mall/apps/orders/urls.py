from django.conf.urls import url

from orders import views


urlpatterns = [
    # 结算
    url(r'^orders/settlement/$', views.OrderSettlementView.as_view()),
    # 提交订单
    url(r'^orders/$', views.CommitOrderView.as_view()),

]
