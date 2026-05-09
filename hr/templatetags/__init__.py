from django import template
register = template.Library()

@register.filter
def get_item(dictionary, key):
    return dictionary.get(key)

@register.filter
def mul(value, arg):
    try:
        return float(value) * float(arg)
    except Exception:
        return ''

@register.filter
def sub(value, arg):
    try:
        return float(value) - float(arg)
    except Exception:
        return ''
