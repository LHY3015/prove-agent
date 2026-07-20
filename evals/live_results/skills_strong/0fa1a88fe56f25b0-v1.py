def extract(text_layout: dict) -> dict:
    import re
    full_text = text_layout['full_text']
    vendor_name = re.search(r'Bill From: (.+?),', full_text).group(1)
    invoice_number = re.search(r'Invoice: (.+)', full_text).group(1)
    invoice_date = re.search(r'Dated: (\d{4}/\d{2}/\d{2})', full_text).group(1)
    currency = re.search(r'Currency: (\w+)', full_text).group(1)
    subtotal = re.search(r'Subtotal (\d+\.\d+)', full_text).group(1)
    tax = re.search(r'Tax \d+\.\d+% (\d+\.\d+)', full_text).group(1)
    total = re.search(r'Total (\d+\.\d+)', full_text).group(1)
    line_items = re.findall(r'\n.+? \d+ x \d+\.\d+ \d+\.\d+', full_text)
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