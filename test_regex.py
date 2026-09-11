import re

s = '&style='
# Use the literal string & as replacement
result = re.sub(r'&(?!amp;|lt;|gt;|quot;|apos;|#)', '&', s)
print(f'Input: {repr(s)}')
print(f'Output: {repr(result)}')