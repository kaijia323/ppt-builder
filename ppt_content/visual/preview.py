"""pptx → PDF + PNG 预览。
LibreOffice 24.x 整套导出时，CJK 字形较多会出现字形错序（PDF 子集字体 bug），
因此逐页拆分转换后再合并。"""
import os, subprocess, sys, tempfile, shutil
from pptx import Presentation


def render(pptx, out_pdf, png_prefix=None, dpi=80):
    n = len(Presentation(pptx).slides)
    tmp = tempfile.mkdtemp()
    parts = []
    for i in range(n):
        p = Presentation(pptx)
        ids = p.slides._sldIdLst
        for j, sid in reversed(list(enumerate(list(ids)))):
            if j != i:
                p.part.drop_rel(sid.rId)
                ids.remove(sid)
        f = os.path.join(tmp, "s%02d.pptx" % (i + 1))
        p.save(f)
        parts.append(f)
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", tmp] + parts,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    pdfs = [f[:-5] + ".pdf" for f in parts]
    subprocess.run(["pdfunite"] + pdfs + [out_pdf], check=True)
    if png_prefix:
        subprocess.run(["pdftoppm", "-r", str(dpi), "-png", out_pdf, png_prefix], check=True)
    shutil.rmtree(tmp)
    return n


if __name__ == "__main__":
    print(render(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
