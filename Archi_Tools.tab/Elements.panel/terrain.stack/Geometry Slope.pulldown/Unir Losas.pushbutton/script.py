# -*- coding: utf-8 -*-
"""Boton pyRevit: une dos losas inclinadas en la arista donde se cruzan."""
import os
import sys

from pyrevit import revit, forms
from Autodesk.Revit import DB
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType

# El paquete archi_losas viaja dentro de la carpeta del boton
_aqui = os.path.dirname(os.path.abspath(__file__))
if _aqui not in sys.path:
    sys.path.insert(0, _aqui)

from archi_losas import revit_io   # noqa: E402

doc, uidoc = revit.doc, revit.uidoc


class _SoloLosas(ISelectionFilter):
    def AllowElement(self, e):
        return isinstance(e, DB.Floor)

    def AllowReference(self, r, p):
        return False


losas = [doc.GetElement(i) for i in uidoc.Selection.GetElementIds()]
losas = [e for e in losas if isinstance(e, DB.Floor)]
if len(losas) != 2:
    try:
        refs = uidoc.Selection.PickObjects(ObjectType.Element, _SoloLosas(), u"Selecciona las dos losas y pulsa Finalizar")
    except Exception:
        sys.exit()
    losas = [doc.GetElement(r) for r in refs]
if len(losas) != 2:
    forms.alert(u"Hay que seleccionar exactamente dos losas.", exitscript=True)

modo = forms.CommandSwitchWindow.show(
    [u"Cumbrera (cada losa conserva el lado donde queda más baja)",
     u"Valle (cada losa conserva el lado donde queda más alta)"],
    message=u"¿Cómo se encuentran las losas?")
if not modo:
    sys.exit()

try:
    A, B = revit_io.calcular(doc, losas[0], losas[1], valle=modo.startswith(u"Valle"))
except ValueError as ex:
    forms.alert(unicode(ex), title=u"Unir losas", exitscript=True)

resumen = u"\n".join(u"Losa %d: %d vértices, %.1f m² -> %.1f m²" % (
    d["losa"].Id.IntegerValue, len(d["nuevo"]),
    abs(revit_io.geom.area(d["pts"])) * 0.09290304, abs(revit_io.geom.area(d["nuevo"])) * 0.09290304)
    for d in (A, B))
if not forms.alert(u"Se reescribirán los contornos; las pendientes no cambian.\n\n%s\n\n"
                   u"Los muros unidos a la losa recortada pueden quedar sin tocarla." % resumen,
                   yes=True, no=True):
    sys.exit()

try:
    revit_io.aplicar(doc, A, B)
except Exception as ex:
    forms.alert(u"No se modificó nada:\n\n%s" % ex, title=u"Unir losas - error")
    raise
forms.alert(u"Listo. Las losas se unen en la arista de cruce.", title=u"Unir losas")
