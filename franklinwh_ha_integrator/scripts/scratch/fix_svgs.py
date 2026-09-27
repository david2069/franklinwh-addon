import re
import sys

def fix_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()
    
    # Replace <use href="#NAME"/> with <use href="static/icons/lucide.svg#NAME"/>
    # But ONLY if it doesn't already have static/icons/lucide.svg
    fixed_content = re.sub(r'href="\#([^"]+)"', r'href="static/icons/lucide.svg#\1"', content)
    
    with open(filepath, 'w') as f:
        f.write(fixed_content)
    print(f"Fixed {filepath}")

fix_file('src/templates/admin.html')
fix_file('src/templates/partials/connection_banner.html')
