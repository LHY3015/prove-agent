def extract(text_layout: dict) -> dict:
    import re
    
    full_text = text_layout['full_text']
    
    vendor_name = re.search(r'Invoice No: \S+ (.+)', full_text).group(1)
    invoice_number = re.search(r'Invoice No: (\S+)', full_text).group(1)
    invoice_date = re.search(r'Date: (\d{4}-\d{2}-\d{2})', full_text).group(1)
    currency = re.search(r'Currency: (\w+)', full_text).group(1)
    subtotal = re.search(r'Subtotal (\S+)', full_text).group(1)
    tax = re.search(r'Tax \(8\.00%\)\s+(\S+)', full_text).group(1)
    total = re.search(r'Total (\S+)', full_text).group(1)
    
    line_items_start = full_text.find('Description Qty Unit Price Amount') + len('Description Qty Unit Price Amount')
    line_items_end = full_text.find('Subtotal')
    line_items_section = full_text[line_items_start:line_items_end].strip()
    line_item_count = str(len(line_items_section.split('\n')))
    
    return {
        'vendor_name': vendor_name,
        'invoice_number': invoice_number,
        'invoice_date': invoice_date,
        'currency': currency,
        'subtotal': subtotal,
        'tax': tax,
        'total': total,
        'line_item_count': line_item_count
    }