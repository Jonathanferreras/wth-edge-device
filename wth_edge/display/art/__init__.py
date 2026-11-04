"""Retro pixel art for the round screen: a tiny drawing kit, the scenes, and the player.

* ``kit``    palette, ASCII sprites, a 3x5 pixel font, dithering and the round mask.
* ``scenes`` the bundled animations (boot, idle, alert, sleep): a mallard on a neon
             cyberpunk canal, drawn in code.
* ``player`` plays a GIF (bundled name or any file) on the display driver.

Scenes are drawn on an 80x80 canvas and scaled 3x to the 240x240 panel, so one art pixel
is a 3x3 block on screen. ``wth-edge art build`` regenerates the GIFs in ``gifs/``.
"""
