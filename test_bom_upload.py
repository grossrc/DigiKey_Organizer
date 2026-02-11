import requests

# Upload the Eagle BOM file
url = "http://127.0.0.1:5000/api/upload_bom"
filepath = "Example BOM files/Schematic v16.csv"

with open(filepath, 'rb') as f:
    files = {'bom': (filepath, f, 'text/csv')}
    response = requests.post(url, files=files)

print(f"Status: {response.status_code}")
print(f"Response: {response.json()}")

if response.ok:
    token = response.json().get('token')
    print(f"\nToken: {token}")
    
    # Fetch results
    results_url = f"http://127.0.0.1:5000/api/bom_results/{token}"
    results_response = requests.get(results_url)
    print(f"\nResults status: {results_response.status_code}")
    
    if results_response.ok:
        results = results_response.json()
        print(f"Filename: {results['filename']}")
        print(f"Warnings: {results['warnings']}")
        print(f"Total lines: {len(results['results'])}")
        print(f"\nFirst 3 results:")
        for i, item in enumerate(results['results'][:3]):
            bom_line = item['bom_line']
            matches = item['matches']
            print(f"\n{i+1}. Designators: {bom_line['designators']}")
            print(f"   Value: {bom_line['value']}, Footprint: {bom_line['footprint']}")
            print(f"   Matches: {len(matches)}")
            if matches:
                print(f"   Best match: {matches[0]['mpn']} ({matches[0]['match_type']}, {matches[0]['confidence']*100:.0f}%)")
