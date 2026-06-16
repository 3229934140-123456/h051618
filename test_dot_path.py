from load_tester.scenario.request import HttpRequest, HttpMethod

req = HttpRequest(
    name='test',
    method=HttpMethod.GET,
    url='http://x.com/${user.product_id}?c=${user.category}',
)
ctx = {'user': {'product_id': '123', 'category': 'books'}}
print('URL:', req.resolve_url(ctx))

req2 = HttpRequest(
    name='test2',
    method=HttpMethod.POST,
    url='http://x.com/orders',
    headers={'X-Trace': 'trace-${trace.id}'},
    body='{"qty": ${user.quantity}, "pay": "${user.payment_method}"}',
)
ctx2 = {
    'trace': {'id': 'abc123'},
    'user': {'quantity': 3, 'payment_method': 'credit_card'},
}
print('Headers:', req2.resolve_headers(ctx2))
print('Body:', req2.resolve_body(ctx2))
