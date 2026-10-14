#!/usr/bin/env python3
"""Generate samples/invoices/inv_pdf_orbit.pdf: a one-page text PDF written by hand (no PDF library needed)."""
import sys

LINES = [
    "ORBIT TRANSLATIONS - INVOICE",
    "From: Orbit Translations",
    "Invoice Number: ORB-1009",
    "Due date: 2026-11-05",
    "",
    "Description                              Amount",
    "Website translation EN-RU    3 x 25.00    75.00",
    "Proofreading                              15.00",
    "",
    "Subtotal                                  90.00 USDC",
    "Total due                                 90.00 USDC",
    "",
    "Network: Pharos mainnet",
    "Pay to wallet: 0x4FFD9c7E8641947662a43aF298B734ca93530002",
]


def pdf(lines):
    esc = lambda s: s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    body = "BT /F1 10 Tf 12 TL 50 780 Td " + " ".join(f"({esc(l)}) Tj T*" for l in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
            f"<< /Length {len(body)} >>\nstream\n{body}\nendstream"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    x = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    out += f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode()
    return out


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "samples/invoices/inv_pdf_orbit.pdf"
    open(path, "wb").write(pdf(LINES))
    print("wrote", path)
