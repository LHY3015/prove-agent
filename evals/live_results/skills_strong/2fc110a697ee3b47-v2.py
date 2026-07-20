def extract(text_layout: dict) -> dict:
    import re
    full_text = text_layout['full_text']
    
    vendor_name = text_layout['lines'][0]
    
    invoice_number_match = re.search(r'Ref (\w+-\d+)', full_text)
    invoice_number = invoice_number_match.group(1) if invoice_number_match else ''
    
    date_currency_match = re.search(r'(\d{4}/\d{2}/\d{2}) / (\w+)', full_text)
    invoice_date = date_currency_match.group(1) if date_currency_match else ''
    currency = date_currency_match.group(2) if date_currency_match else ''
    
    subtotal_match = re.search(r'Subtotal\s+(\d+\.\d{2})', full_text)
    subtotal = subtotal_match.group(1) if subtotal_match else ''
    
    tax_match = re.search(r'Tax \d+\.\d+% (\d+\.\d{2})', full_text)
    tax = tax_match.group(1) if tax_match else ''
    
    total_match = re.search(r'TOTAL (\d+\.\d{2})', full_text)
    total = total_match.group(1) if total_match else ''
    
    line_items = re.findall(r'^[^S].*?\s+\d+x\s+\d+\.\d{2}\s+\d+\.\d{2}$', full_text, re.MULTILINE)
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