"""Post-process pandoc's .docx: settings.xml children in schema order plus updateFields (Word fills in the TOC on
open), m:count before m:mcJc in matrix columns, and a png Default content type. Usage: fix_docx.py FILE.docx"""
import re, shutil, sys, tempfile, zipfile
from pathlib import Path
from lxml import etree

ORDER = """writeProtection view zoom removePersonalInformation removeDateAndTime doNotDisplayPageBoundaries
displayBackgroundShape printPostScriptOverText printFractionalCharacterWidth printFormsData embedTrueTypeFonts
embedSystemFonts saveSubsetFonts saveFormsData mirrorMargins alignBordersAndEdges bordersDoNotSurroundHeader
bordersDoNotSurroundFooter gutterAtTop hideSpellingErrors hideGrammaticalErrors activeWritingStyle proofState
formsDesign attachedTemplate linkStyles stylePaneFormatFilter stylePaneSortMethod documentType mailMerge revisionView
trackRevisions doNotTrackMoves doNotTrackFormatting documentProtection autoFormatOverride styleLockTheme styleLockQFSet
defaultTabStop autoHyphenation consecutiveHyphenLimit hyphenationZone doNotHyphenateCaps showEnvelope summaryLength
clickAndTypeStyle defaultTableStyle evenAndOddHeaders bookFoldRevPrinting bookFoldPrinting bookFoldPrintingSheets
drawingGridHorizontalSpacing drawingGridVerticalSpacing displayHorizontalDrawingGridEvery
displayVerticalDrawingGridEvery doNotUseMarginsForDrawingGridOrigin drawingGridHorizontalOrigin
drawingGridVerticalOrigin doNotShadeFormData noPunctuationKerning characterSpacingControl printTwoOnOne
strictFirstAndLastChars noLineBreaksAfter noLineBreaksBefore savePreviewPicture doNotValidateAgainstSchema
saveInvalidXml ignoreMixedContent alwaysShowPlaceholderText doNotDemarcateInvalidXml saveXmlDataOnly useXSLTWhenSaving
saveThroughXslt showXMLTags alwaysMergeEmptyNamespace updateFields hdrShapeDefaults footnotePr endnotePr compat docVars
rsids mathPr attachedSchema themeFontLang clrSchemeMapping doNotIncludeSubdocsInStats doNotAutoCompressPictures
forceUpgrade captions readModeInkLockDown smartTagType schemaLibrary shapeDefaults doNotEmbedSmartTags decimalSymbol
listSeparator""".split()
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

path = Path(sys.argv[1])
tmp = Path(tempfile.mkdtemp())
with zipfile.ZipFile(path) as z:
    z.extractall(tmp)
s = tmp / "word/settings.xml"
t = etree.parse(str(s)); r = t.getroot()
if r.find(f"{{{W}}}updateFields") is None:
    etree.SubElement(r, f"{{{W}}}updateFields").set(f"{{{W}}}val", "true")
rank = {n: i for i, n in enumerate(ORDER)}
kids = sorted(list(r), key=lambda e: rank.get(etree.QName(e).localname, 999))
for k in list(r):
    r.remove(k)
for k in kids:
    r.append(k)
t.write(str(s), xml_declaration=True, encoding="UTF-8", standalone=True)
d = tmp / "word/document.xml"
d.write_text(re.sub(r'(<m:mcJc m:val="[a-z]+" />)(<m:count m:val="[0-9]+" />)', r"\2\1", d.read_text("utf8")), "utf8")
ct = tmp / "[Content_Types].xml"
x = ct.read_text("utf8")
if 'Extension="png"' not in x:
    x = x.replace('<Default Extension="xml"', '<Default Extension="png" ContentType="image/png" /><Default Extension="xml"', 1)
ct.write_text(x, "utf8")
path.unlink()
with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:   # files only, no folder entries
    for f in sorted(tmp.rglob("*")):
        if f.is_file():
            z.write(f, f.relative_to(tmp).as_posix())
shutil.rmtree(tmp)
