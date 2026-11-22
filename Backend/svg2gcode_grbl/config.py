"""G-code emitted at the start of processing the SVG file"""
preamble = "G90"


"""G-code emitted at the end of processing the SVG file"""
postamble = "(postamble)"

"""G-code emitted before processing a SVG shape"""
shape_preamble = "(shape preamble)"
#shape_preamble = "Z0"

"""G-code emitted after processing a SVG shape"""
shape_postamble = "(shape postamble)"
#shape_postamble = "Z100)"

"""Print bed width in mm"""
bed_max_x = 300

"""Print bed height in mm"""
bed_max_y = 350
placement_x = 0.5
placement_y = 0.5

""" 
Used to control the smoothness/sharpness of the curves.
Smaller the value greater the sharpness. Make sure the
value is greater than 0.1

"""
smoothness = 0.02

""" height that the z axis will use to travel between strokes """
zTravel = 4

""" height that the z axis will use to draw """
zDraw = 0

"""conservative commissioning feed rate in millimeters per minute"""
feed_rate = 300

"""pen lift mode: z axis or FluidNC PWM servo"""
pen_lift_mode = "z"

"""initial FluidNC PWM servo positions; calibrate before plotting"""
servo_up_s = 700
servo_down_s = 800
servo_delay_ms = 300

""" decimal precision of gcode"""
precision = 2


""" scale gcode to fit bed size"""
auto_scale = False

""" optimize path - slow for large files"""
optimise = True

""" 
illustrator exports svg's in points, not mm
set to "mm" if you don't want to convert to mm
"""
units = "points"
#units = "mm"