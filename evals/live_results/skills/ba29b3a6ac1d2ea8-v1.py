def extract(text_layout: dict) -> dict:
    import re
    
    full_text = text_layout["full_text"]
    lines = text_layout["lines"]
    
    vendor_name = lines[0] if lines else ""
    invoice_number = next((line for line in lines if line.startswith("#")), "")
    invoice_date = next((line for line in lines if re.match(r"\d{2}/\d{2}/\d{4}", line)), "")
    currency = next((line.split()[-1] for line in lines if line.startswith("Paid in")), "")
    
    subtotal = next((line.split()[-1] for line in lines if line.startswith("Subtotal")), "")
    tax = next((line.split()[-1] for line in lines if line.startswith("Tax")), "")
    total = next((line.split()[-1] for line in lines if line.startswith("Amount Due")), "")
    
    line_item_count = str(len([line for line in lines if re.match(r".+ \d+ \d+\.\d+ \d+\.\d+", line)]))
    
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