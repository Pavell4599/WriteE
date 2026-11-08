from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
import xml.etree.ElementTree as ET
import random

def generate_handwriting_svg(text, ttf_path, output_filename="result.svg"):
    # 1. Загружаем сгенерированный .ttf шрифт
    font = TTFont(ttf_path)
    glyph_set = font.getGlyphSet()
    cmap = font.getBestCmap() # Таблица соответствия символов юникода глифам
    
    # 2. Создаем корневой SVG-документ
    svg = ET.Element('svg', {
        'xmlns': 'http://www.w3.org/2000/svg',
        'version': '1.1',
        'width': '1200',
        'height': '800'
    })
    
    # Начальные координаты курсора на странице
    cursor_x = 50
    cursor_y = 150
    
    # Коэффициент масштабирования (шрифты обычно используют большие юниты вроде 1000x1000, 
    # поэтому их нужно уменьшить под пиксели/миллиметры SVG)
    scale = 0.04 
    
    line_height = 60
    space_width = 30
    
    for char in text:
        # Обработка переноса строк
        if char == '\n':
            cursor_x = 50
            cursor_y += line_height
            continue
            
        # Обработка пробелов
        if char == ' ':
            cursor_x += space_width
            continue
            
        char_code = ord(char)
        
        # Проверяем, есть ли такой символ в вашем шрифте
        if char_code in cmap:
            glyph_name = cmap[char_code]
            
            # 3. Достаем векторный путь (описания кривых) конкретной буквы
            pen = SVGPathPen(glyph_set)
            glyph_set[glyph_name].draw(pen)
            path_data = pen.getCommands()
            
            # 4. Добавляем легкий рандомный сдвиг (джиттер) для эффекта «живого» рукописного почерка
            jitter_x = random.uniform(-0.8, 0.8)
            jitter_y = random.uniform(-1.2, 1.2)
            
            # Внимание: В шрифтах ось Y смотрит ВВЕРХ, а в SVG — ВНИЗ. 
            # Поэтому в трансформации scale мы инвертируем Y (-scale), чтобы буквы не были вверх ногами.
            transform_str = f"translate({cursor_x + jitter_x}, {cursor_y + jitter_y}) scale({scale}, -{scale})"
            
            # Создаем тег <path> для каждой буквы
            ET.SubElement(svg, 'path', {
                'd': path_data,
                'fill': 'none',          # Для плоттера нужен контур, а не заливка ('black' если нужен залитый текст)
                'stroke': 'black',
                'stroke-width': '1.5',
                'transform': transform_str
            })
            
            # Смещаем курсор вправо для следующей буквы (упрощенно на фиксированную ширину)
            cursor_x += 28
            
        else:
            # Если символ не найден в шрифте, просто пропускаем или ставим отступ
            cursor_x += 20

    # Сохраняем готовый XML в файл .svg
    tree = ET.ElementTree(svg)
    tree.write(output_filename, encoding='utf-8', xml_declaration=True)
    print(f"Готово! Файл сохранен как {output_filename}")

# --- Пример использования ---
# generate_handwriting_svg("Привет, мир!\nЭто тест почерка.", "my_font.ttf", "output.svg")