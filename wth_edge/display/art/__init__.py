"""Retro pixel art for the round screen: a tiny drawing kit, the scene, the animations, the player.

* ``kit``    palette, ASCII sprites, a 3x5 pixel font, dithering and the round mask.
* ``lofi``   the lofi cyberpunk canal: Officer Garrison on the patrol car, Eugene in the
             water, the drawbridge, traffic, weather. ``draw(t, look)`` returns one frame.
* ``scenes`` the bundled animations (boot, idle, alert, sleep) built from the scene.
* ``live``   turns the device state into the scene's look, so the art is the status screen.
* ``player`` plays a GIF (bundled name or any file) on the display driver.

The scene is drawn on a 120x120 canvas and scaled 2x to the 240x240 panel, so one art pixel
is a 2x2 block on screen. ``wth-edge art build`` regenerates the GIFs in ``gifs/``.
"""
