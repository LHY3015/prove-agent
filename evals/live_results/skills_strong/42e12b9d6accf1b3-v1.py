def extract(text_layout: dict) -> dict:
    import re
    
    lines = text_layout.get('lines', [])
    
    vendor_name = lines[0] if lines else ""
    invoice_number_match = re.search(r'Ref (\w+-\d+)', "\n".join(lines))
    invoice_number = invoice_number_match.group(1) if invoice_number_match else ""
    
    date_currency_match = re.search(r'(\w{3} \d{2} \d{4}) / (\w{3})', "\n".join(lines))
    invoice_date = date_currency_match.group(1) if date_currency_match else ""
    currency = date_currency_match.group(2) if date_currency_match else ""
    
    subtotal_match = re.search(r'Subtotal (\d+\.\d{2})', "\n".join(lines))
    subtotal = subtotal_match.group(1) if subtotal_match else ""
    
    tax_match = re.search(r'Tax \d+\.\d{2}% (\d+\.\d{2})', "\n".join(lines))
    tax = tax_match.group(1) if tax_match else ""
    
    total_match = re.search(r'TOTAL (\d+\.\d{2})', "\n".join(lines))
    total = total_match.group(1) if total_match else ""
    
    line_items = [line for line in lines if re.match(r'.+ \d+ x \d+\.\d{2} \d+\.\d{2}', line)]
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