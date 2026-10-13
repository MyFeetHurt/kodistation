"""Generate resources/icon.png (256x256): a TV with a red record dot."""
from PIL import Image, ImageDraw

S = 512  # draw at 2x, downsample for smooth edges
img = Image.new("RGBA", (S, S), (18, 32, 47, 255))
d = ImageDraw.Draw(img)
d.rounded_rectangle((56, 110, 456, 380), radius=36, outline=(18, 180, 230), width=26)
d.line((196, 440, 316, 440), fill=(18, 180, 230), width=26)
d.line((256, 380, 256, 440), fill=(18, 180, 230), width=26)
d.ellipse((186, 175, 326, 315), fill=(230, 40, 50))
img.resize((256, 256), Image.LANCZOS).save("plugin.video.epgstation/resources/icon.png")
