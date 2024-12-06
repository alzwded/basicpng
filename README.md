# basicpng

Pure python PNG decoder.

Usage:

```python
from basicpng import PngDecode

# Load PNG
D = PngDecode("./my.png")
# Grab the extreme corner's U8 RGBA values
x = D.w - 1
y = D.h - 1
(r, g, b, a) = D.get(x, y)
print(r, g, b)
```

What it does:

- Parse `IHDR`, `PLTE`, `IDAT` and `IEND` chunks. Others are ignored entirely
- Supports 1-16 bit per channel formats, including indexed color, but 16bits/channel formats get reduced to 8bits/channel
- Only depends on the base Python distribution + zlib. There are cases where this is important...

Limitations:

- Only progressive scan images are supported. Adam7 is not.
- 16bits/channel formats get reduced to 8bits/channel.
- Transparency is read only from color types 4 and 6, fancy ancillary chunks are ignored.
- Generally, no ancillary chunks are processed.
- Probably a lot slower than libpng, I didn't even try to put the head-to-head `:-)`

In case you're wondering how you can generate test images with indexed-color or gray scale, here are the ImageMagick incantations:

```
convert ref.png -colors 16 -type Palette indexed.png
convert ref.png -colorspace Gray grayscale.png
```

`identify` will report the amount of colors and that it has one channel.
