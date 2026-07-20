def extract(text_layout: dict) -> dict:
    import re
    
    full_text = text_layout['full_text']
    
    vendor_name = full_text.split('\n')[0]
    
    invoice_number_match = re.search(r'Invoice # (\w+-\d+)', full_text)
    invoice_number = invoice_number_match.group(1) if invoice_number_match else ''
    
    invoice_date_match = re.search(r'Date (\d{2}/\d{2}/\d{4})', full_text)
    invoice_date = invoice_date_match.group(1) if invoice_date_match else ''
    
    currency_match = re.search(r'Currency (\w+)', full_text)
    currency = currency_match.group(1) if currency_match else ''
    
    subtotal_match = re.search(r'Subtotal (\d+\.\d{2})', full_text)
    subtotal = subtotal_match.group(1) if subtotal_match else ''
    
    tax_match = re.search(r'Tax \(\d+\.\d{2}%\) (\d+\.\d{2})', full_text)
    tax = tax_match.group(1) if tax_match else ''
    
    total_match = re.search(r'Total (\d+\.\d{2})', full_text)
    total = total_match.group(1) if total_match else ''
    
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