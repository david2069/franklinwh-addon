import re

def revert_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()
    
    # Revert static/icons/lucide.svg#NAME back to #NAME
    reverted_content = content.replace('href="static/icons/lucide.svg#', 'href="#')
    
    with open(filepath, 'w') as f:
        f.write(reverted_content)
    print(f"Reverted {filepath}")

revert_file('src/templates/admin.html')
revert_file('src/templates/partials/connection_banner.html')
