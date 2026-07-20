def extract(text_layout: dict) -> dict:
    import re

    full_text = text_layout['full_text']
    lines = text_layout['lines']

    vendor_name = lines[0] if lines else ""
    invoice_number_match = re.search(r'Number\s+(\S+)', full_text)
    invoice_number = invoice_number_match.group(1) if invoice_number_match else ""
    invoice_date_match = re.search(r'Issued\s+([\w\s,]+)', full_text)
    invoice_date = invoice_date_match.group(1).strip() if invoice_date_match else ""
    currency_match = re.search(r'Currency\s+(\w+)', full_text)
    currency = currency_match.group(1) if currency_match else ""
    subtotal_match = re.search(r'Subtotal\s+(\d+\.\d{2})', full_text)
    subtotal = subtotal_match.group(1) if subtotal_match else ""
    tax_match = re.search(r'Tax\s+\d+\.?\d*%\s+(\d+\.\d{2})', full_text)
    tax = tax_match.group(1) if tax_match else ""
    total_match = re.search(r'Total Due\s+(\d+\.\d{2})', full_text)
    total = total_match.group(1) if total_match else ""

    line_items_start = lines.index("Item Qty Rate Line Total") + 1 if "Item Qty Rate Line Total" in lines else 0
    line_items_end = next((i for i, line in enumerate(lines[line_items_start:], line_items_start) if line.startswith("Subtotal")), len(lines))
    line_item_count = str(line_items_end - line_items_start)

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