def extract(text_layout: dict) -> dict:
    lines = text_layout.get('lines', [])
    full_text = text_layout.get('full_text', '')

    vendor_name = lines[0] if lines else ''
    invoice_number = ''
    invoice_date = ''
    currency = ''
    subtotal = ''
    tax = ''
    total = ''
    line_item_count = '0'

    for line in lines:
        if line.startswith('Ref '):
            invoice_number = line.split(' ')[1]
        elif ' / ' in line:
            parts = line.split(' / ')
            invoice_date = parts[0]
            currency = parts[1]
        elif line.startswith('Subtotal'):
            subtotal = line.split(' ')[1]
        elif line.startswith('Tax'):
            tax = line.split(' ')[2]
        elif line.startswith('TOTAL'):
            total = line.split(' ')[1]

    line_items = [line for line in lines if len(line.split()) > 3 and line.split()[0].isdigit()]
    line_item_count = str(len(line_items))

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