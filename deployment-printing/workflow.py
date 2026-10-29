"""Ticket payloads for committed database events. No browser polling of orders."""
def build_test_payload(restaurant, kind, now):
    """Use the existing 60x40 renderer without creating a business order or payment."""
    return {'workflow': 1, 'is_test': True, 'event_ids': [],
            'title': '后厨测试单' if kind == 'kitchen' else '结算测试单',
            'order_id': 'TEST', 'table_name': '测试桌',
            'restaurant_name': restaurant['name'],
            'currency_code': restaurant.get('currency_code') or 'CNY',
            'timezone': restaurant.get('timezone') or 'Asia/Shanghai',
            'created_at': now.isoformat(), 'paid_at': now.isoformat() if kind == 'receipt' else None,
            'payment_method': None, 'payments': [], 'order_notes': '仅测试，不制作、不收款',
            'tip_amount_cents': 0, 'loyalty_discount_cents': 0, 'delivery_fee_cents': 0,
            'total_cents': 0,
            'lines': [{'name': '打印测试 ABC 123', 'quantity': 1, 'price_cents': 0,
                       'action': '测试', 'notes': ''}]}


def active(item):
    return not item.get('removed_by_customer') and item.get('removed_by_user_id') is None and item.get('status') != 'cancelled'


def item_line(item, quantity=None, action='制作'):
    return {'name': item.get('name') or item.get('product_name') or '未命名菜品',
            'quantity': item['quantity'] if quantity is None else quantity,
            'price_cents': int(item.get('price_cents') or 0), 'action': action,
            'notes': '；'.join(str(item[k]) for k in ('notes','customization_summary','line_modifiers_summary') if item.get(k))}


def build_payload(events):
    latest = events[-1]['payload']
    order = latest['order']
    kind = events[0]['kind']
    lines = []
    if kind == 'receipt':
        lines = [item_line(i) for i in latest['items'] if active(i)]
        total = max(0, sum(i['price_cents'] * i['quantity'] for i in lines)
                    + int(order.get('delivery_fee_cents') or 0) - int(order.get('loyalty_discount_cents') or 0)) + int(order.get('tip_amount_cents') or 0)
        title = '结算单 · 已付款'
    elif kind == 'cancel':
        lines = [item_line(i, action='退菜') for i in latest['items'] if active(i)]
        total = 0
        title = '后厨退菜单'
    else:
        # Preserve event order: quantity deltas and changed notes are explicit, never a full-order reprint.
        for event in events:
            new, old = event['payload']['new'], event['payload'].get('old')
            previous = int(old['quantity']) if old and active(old) else 0
            current = int(new['quantity']) if active(new) else 0
            changed = old and any(old.get(k) != new.get(k) for k in ('product_name','notes','customization_summary','line_modifiers_summary'))
            if changed and previous and current:
                lines.append(item_line(old, previous, '撤销原项'))
                lines.append(item_line(new, current, '改菜'))
            elif current != previous:
                lines.append(item_line(new if current > previous else old, abs(current - previous), '加菜' if current > previous else '退菜'))
        total = 0
        title = '后厨单'
    if not lines:
        return None
    return {'workflow': 1, 'event_ids': [e['id'] for e in events], 'title': title,
            'order_id': order['id'], 'table_name': order.get('table_name'),
            'restaurant_name': latest['tenant']['name'],
            'currency_code': latest['tenant'].get('currency_code') or 'CNY',
            'timezone': latest['tenant'].get('timezone') or 'Asia/Shanghai',
            'created_at': order.get('created_at'), 'paid_at': order.get('paid_at'),
            'payment_method': order.get('payment_method'), 'payments': latest.get('payments', []),
            'order_notes': '\n'.join(s for s in (order.get('notes') or '').splitlines() if not s.strip().upper().startswith('[PAID:')),
            'tip_amount_cents': order.get('tip_amount_cents') or 0,
            'loyalty_discount_cents': order.get('loyalty_discount_cents') or 0,
            'delivery_fee_cents': order.get('delivery_fee_cents') or 0,
            'total_cents': total, 'lines': lines}
