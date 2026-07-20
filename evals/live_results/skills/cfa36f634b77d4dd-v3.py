def extract(text_layout: dict) -> dict:
    import re
    full_text = text_layout["full_text"]
    
    vendor_name = full_text.split('\n')[0]
    
    invoice_number_match = re.search(r'Invoice # (\S+)', full_text)
    invoice_number = invoice_number_match.group(1) if invoice_number_match else ""
    
    invoice_date_match = re.search(r'Date (\S+)', full_text)
    invoice_date = invoice_date_match.group(1) if invoice_date_match else ""
    
    currency_match = re.search(r'Currency (\S+)', full_text)
    currency = currency_match.group(1) if currency_match else ""
    
    subtotal_match = re.search(r'Subtotal (\S+)', full_text)
    subtotal = subtotal_match.group(1) if subtotal_match else ""
    
    tax_match = re.search(r'Tax \(\S+\) (\S+)', full_text)
    tax = tax_match.group(1) if tax_match else ""
    
    total_match = re.search(r'Total (\S+)', full_text)
    total = total_match.group(1) if total_match else ""
    
    lines = full_text.split('\n')
    start_index = lines.index("Description Qty Unit Price Amount") + 1
    end_index = lines.index("Subtotal " + subtotal)
    line_item_count = str(end_index - start_index)
    
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