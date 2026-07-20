def extract(text_layout: dict) -> dict:
    import re

    full_text = text_layout.get('full_text', '')
    lines = text_layout.get('lines', [])

    vendor_name = lines[0] if lines else ''
    invoice_number_match = re.search(r'Ref\s+(\w+-\d+)', full_text)
    invoice_number = invoice_number_match.group(1) if invoice_number_match else ''
    date_currency_match = re.search(r'(\d{4}/\d{2}/\d{2})\s+/\s+(\w+)', full_text)
    invoice_date = date_currency_match.group(1) if date_currency_match else ''
    currency = date_currency_match.group(2) if date_currency_match else ''

    subtotal_match = re.search(r'Subtotal\s+(\d+\.\d{2})', full_text)
    subtotal = subtotal_match.group(1) if subtotal_match else ''
    tax_match = re.search(r'Tax\s+\d+\.\d{2}%\s+(\d+\.\d{2})', full_text)
    tax = tax_match.group(1) if tax_match else ''
    total_match = re.search(r'TOTAL\s+(\d+\.\d{2})', full_text)
    total = total_match.group(1) if total_match else ''

    line_items = [line for line in lines if re.match(r'.*\sx\s+\d+\.\d{2}\s+\d+\.\d{2}', line)]
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