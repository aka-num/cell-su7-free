"""Local font-advance estimation for independent editable SVG text spans."""
from functools import lru_cache
from PIL import ImageFont

@lru_cache(maxsize=128)
def _font(family,size):
    name=family.lower()
    if 'mono' in name:preferred='DejaVuSansMono.ttf'
    elif 'serif' in name and 'sans' not in name:preferred='DejaVuSerif.ttf'
    else:preferred='DejaVuSans.ttf'
    choices=[family,family+'.ttf',preferred]
    for path in choices:
        try:return ImageFont.truetype(path,max(1,round(size)))
        except (OSError,ValueError):pass
    return ImageFont.load_default()

def advance(text,size,family):
    if not text:return 0
    try:return _font(str(family),int(max(1,size))).getlength(text)*size/max(1,round(size))
    except Exception:return len(text)*size*.55
