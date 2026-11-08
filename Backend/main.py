import xml.etree.ElementTree as ET
import random

class HandwritingSVGGenerator:
    def __init__(self, font_glyphs_path):
        """
        font_glyphs_path: словарь, где ключ — символ ('а', 'б', 'A'), 
        а значение — SVG path d="..." соответствующей буквы.
        """
        self.glyphs = font_glyphs_path
        self.letter_spacing = 15  # Расстояние между буквами (в мм/пикселях)
        self.line_height = 30     # Высота строки
        self.space_width = 20     # Ширина пробела

    def generate_svg(self, text, output_filename="handwritten_output.svg"):
        svg_elem = ET.Element('svg', {
            'xmlns': 'http://www.w3.org/2000/svg',
            'version': '1.1',
            'width': '800',
            'height': '600'
        })
        
        cursor_x = 50
        cursor_y = 50
        
        for char in text:
            if char == '\n':
                cursor_x = 50
                cursor_y += self.line_height
                continue
            
            if char == ' ':
                cursor_x += self.space_width
                continue
                
            if char in self.glyphs:
                d_path = self.glyphs[char]
                
                # Добавляем небольшой случайный сдвиг (джиттер) для "живого" почерка
                jitter_x = random.uniform(-0.5, 0.5)
                jitter_y = random.uniform(-0.8, 0.8)
                
                # Создаем элемент пути для буквы
                path_elem = ET.SubElement(svg_elem, 'path', {
                    'd': d_path,
                    'fill': 'none',
                    'stroke': 'black',
                    'stroke-width': '1.5',
                    'stroke-linecap': 'round',
                    'stroke-linejoin': 'round',
                    'transform': f'translate({cursor_x + jitter_x:.2f}, {cursor_y + jitter_y:.2f})'
                })
                
                cursor_x += self.letter_spacing

        tree = ET.ElementTree(svg_elem)
        tree.write(output_filename, encoding='utf-8', xml_declaration=True)
        print(f"Файл {output_filename} успешно сохранен!")

# Пример использования:
# Словарь с SVG-контурами single-stroke букв
sample_glyphs = {
    'П': 'M 0 20 L 0 0 L 10 0 L 10 20',
    'р': 'M 0 20 L 0 -5 M 0 0 C 5 0, 10 5, 5 10 C 0 10, 0 10, 0 10',
    'и': 'M 0 0 L 0 15 L 10 0 L 10 15',
    'в': 'M 0 0 L 0 15 C 5 15, 8 10, 0 8 C 5 8, 8 0, 0 0',
    'е': 'M 0 8 L 8 8 C 8 0, 0 0, 0 8 C 0 15, 8 15, 8 10',
    'т': 'M 0 0 L 10 0 M 5 0 L 5 15'
}

generator = HandwritingSVGGenerator(sample_glyphs)
generator.generate_svg("Привет\nПривет", "plotter_output.svg")