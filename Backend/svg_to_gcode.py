import xml.etree.ElementTree as ET
import re

def parse_svg_path(path_string):
    """
    Разбирает SVG Path (команды M, m, L, l, H, h, V, v, Z, z).
    Возвращает список контуров: [[(x1, y1), (x2, y2)], [(x3, y3), ...]]
    """
    path_string = re.sub(r'([MmLlHhVvZz,])', r' \1 ', path_string)
    tokens = path_string.split()
    
    contours = []
    current_contour = []
    curr_x, curr_y = 0.0, 0.0
    start_x, start_y = 0.0, 0.0
    i = 0
    cmd = ''
    
    while i < len(tokens):
        token = tokens[i]
        if token.isalpha():
            cmd = token
            i += 1
            
        if cmd in ['M', 'm', 'L', 'l']:
            if i + 1 >= len(tokens): break
            val_x, val_y = float(tokens[i]), float(tokens[i+1])
            i += 2
            if cmd == 'M':
                curr_x, curr_y = val_x, val_y
                start_x, start_y = curr_x, curr_y
                if current_contour: contours.append(current_contour)
                current_contour = [(curr_x, curr_y)]
                cmd = 'L'
            elif cmd == 'm':
                curr_x += val_x; curr_y += val_y
                start_x, start_y = curr_x, curr_y
                if current_contour: contours.append(current_contour)
                current_contour = [(curr_x, curr_y)]
                cmd = 'l'
            elif cmd == 'L':
                curr_x, curr_y = val_x, val_y
                current_contour.append((curr_x, curr_y))
            elif cmd == 'l':
                curr_x += val_x; curr_y += val_y
                current_contour.append((curr_x, curr_y))
        elif cmd in ['H', 'h']:
            if i >= len(tokens): break
            val = float(tokens[i]); i += 1
            curr_x = val if cmd == 'H' else curr_x + val
            current_contour.append((curr_x, curr_y))
        elif cmd in ['V', 'v']:
            if i >= len(tokens): break
            val = float(tokens[i]); i += 1
            curr_y = val if cmd == 'V' else curr_y + val
            current_contour.append((curr_x, curr_y))
        elif cmd in ['Z', 'z']:
            curr_x, curr_y = start_x, start_y
            current_contour.append((curr_x, curr_y))
            cmd = ''
            if current_contour:
                contours.append(current_contour)
                current_contour = []
        else:
            i += 1 # Пропуск кривых Безье (C, S, Q) для упрощения (в реальном приложении нужна аппроксимация)
            
    if current_contour: contours.append(current_contour)
    return contours

def svg_to_gcode(svg_file_path, gcode_file_path, z_up=5.0, z_down=0.0, feed_rate=1500):
    tree = ET.parse(svg_file_path)
    root = tree.getroot()
    namespaces = {'svg': 'http://w3.org'}
    paths = root.findall('.//svg:path', namespaces) or root.findall('.//path')
    
    gcode = [
        "G21 ; Метрическая система",
        "G90 ; Абсолютные координаты",
        f"G0 Z{z_up:.2f} ; Поднять перо",
        f"G92 X0 Y0 Z{z_up:.2f} ; Обнулить оси",
    ]
    
    for idx, path in enumerate(paths):
        d_attr = path.get('d')
        if not d_attr: continue
        
        contours = parse_svg_path(d_attr)
        for contour in contours:
            if not contour: continue
            # Перемещение к первой точке буквы
            gcode.append(f"G0 X{contour[0][0]:.3f} Y{contour[0][1]:.3f} Z{z_up:.2f}")
            # Опускание
            gcode.append(f"G1 Z{z_down:.2f} F{feed_rate}")
            # Рисование
            for pt in contour[1:]:
                gcode.append(f"G1 X{pt[0]:.3f} Y{pt[1]:.3f}")
            # Подъем
            gcode.append(f"G0 Z{z_up:.2f}")
            
    gcode.extend(["G0 X0 Y0 ; Домой", "M84 ; Отключить моторы"])
    
    with open(gcode_file_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(gcode))
    


if __name__ == '__main__':
    svg_to_gcode('PAVEL/usage_demo.svg', 'PAVEL/test_gcode_1.gcode')