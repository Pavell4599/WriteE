import sys
import xml.etree.ElementTree as ET

path = "./svg/example.svg"

with open("./svg/example.svg") as file:
    data = file.read()




tree = ET.parse(path)


root = tree.getroot()

for i in root:
    print(i.attrib)

# tree = ET.parse(data)
# print(tree)

#print(ET.parse(sys.stdin))