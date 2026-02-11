from bom_extract import parse_bom_csv

# Test Eagle format
content = open('Example BOM files/Schematic v16.csv', 'r', encoding='utf-8').read()
lines, warnings = parse_bom_csv(content)

print(f'Lines parsed: {len(lines)}')
print(f'Warnings: {warnings[:5]}')
print('\nFirst 10 lines:')
for i, l in enumerate(lines[:10]):
    des = l.designators[0] if l.designators else '?'
    print(f'{i}: {des:10} value={l.value:20} footprint={l.footprint}')
