def extract(text_layout: dict) -> dict:
    import re
    full_text = text_layout['full_text']
    vendor_name = full_text.split('\n')[0]
    invoice_number = re.search(r'Invoice # (\S+)', full_text).group(1)
    invoice_date = re.search(r'Date (\S+ \d{1,2}, \d{4})', full_text).group(1)
    currency = re.search(r'Currency (\w+)', full_text).group(1)
    subtotal = re.search(r'Subtotal (\S+)', full_text).group(1)
    tax = re.search(r'Tax \(\S+\) (\S+)', full_text).group(1)
    total = re.search(r'Total (\S+)', full_text).group(1)
    line_items = full_text.split('Description Qty Unit Price Amount')[1].split('Subtotal')[0].strip().split('\n')
    line_item_count = str(len(line_items))
    return {
        "vendor_name": vendor_name,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "currency": currency,
        "subtotal": subtotal,
        "tax": tax,
        "total": total,
        "line_item_count": line_item_count
    }