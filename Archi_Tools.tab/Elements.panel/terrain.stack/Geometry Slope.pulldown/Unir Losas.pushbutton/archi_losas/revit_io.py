# -*- coding: utf-8 -*-
"""Une dos losas con pendiente distinta en su arista de cruce. IronPython 2.7.

Cada losa conserva su pendiente: solo cambia el perimetro. Toda la zona
combinada se reparte con la recta donde se cruzan los planos superiores:
cada losa se queda con el lado donde ella es la mas baja (cumbrera) o la mas
alta (valle). Como la recta es la unica frontera, la junta no tiene escalon.
"""
import clr
clr.AddReference("RevitAPI")
from Autodesk.Revit import DB
from archi_losas import geom

BIP = DB.BuiltInParameter
# Orden obligatorio: en una linea nueva solo DEFINES_CONSTANT_HEIGHT es editable;
# al activarlo se habilitan los otros tres (si no: "The parameter is read-only").
_PARAMS_PENDIENTE = (BIP.DEFINES_CONSTANT_HEIGHT, BIP.CURVE_IS_SLOPE_DEFINING,
                     BIP.ROOF_SLOPE, BIP.CURVE_HEIGHT_OFFSET)
TOL_PLANO = 1e-6


class _Avisos(DB.IFailuresPreprocessor):
    """Los avisos de union/solape no deben abrir un dialogo modal.

    Los errores con resolucion (p. ej. "Can't keep wall and floor joined") se
    resuelven con la accion por defecto de Revit: separar los elementos.
    """
    def PreprocessFailures(self, acc):
        resuelto = False
        for m in list(acc.GetFailureMessages()):
            sev = m.GetSeverity()
            if sev == DB.FailureSeverity.Warning:
                acc.DeleteWarning(m)
            elif sev == DB.FailureSeverity.Error and m.HasResolutions():
                acc.ResolveFailure(m)
                resuelto = True
        return DB.FailureProcessingResult.ProceedWithCommit if resuelto else DB.FailureProcessingResult.Continue


def _plano_superior(doc, losa):
    cara = losa.GetGeometryObjectFromReference(DB.HostObjectUtils.GetTopFaces(losa)[0])
    n, o = cara.FaceNormal, cara.Origin
    return geom.plano_desde_cara(n.X, n.Y, n.Z, o.X, o.Y, o.Z)


def _leer(doc, losa):
    """Contorno, plano y linea que define la pendiente de una losa."""
    sk = doc.GetElement(losa.SketchId)
    lazos = [list(lz) for lz in sk.Profile]
    if not lazos or any(not isinstance(c, DB.Line) for lz in lazos for c in lz):
        raise ValueError(u"La losa %d debe tener solo contornos de lineas rectas." % losa.Id.IntegerValue)
    todos = _ordenar([[(c.GetEndPoint(0).X, c.GetEndPoint(0).Y) for c in lz] for lz in lazos])
    pts, huecos = todos[0], todos[1:]
    z = lazos[0][0].GetEndPoint(0).Z
    pend = [doc.GetElement(i) for i in sk.GetAllElements()]
    pend = [e for e in pend if isinstance(e, DB.ModelCurve)
            and e.get_Parameter(BIP.CURVE_IS_SLOPE_DEFINING).AsInteger() == 1]
    if len(pend) != 1:
        raise ValueError(u"La losa %d debe tener exactamente una linea que define la pendiente." % losa.Id.IntegerValue)
    c = pend[0].GeometryCurve
    return {"losa": losa, "sk": sk, "pts": pts, "huecos": huecos, "z": z, "plano": _plano_superior(doc, losa),
            "lin": pend[0], "a": (c.GetEndPoint(0).X, c.GetEndPoint(0).Y),
            "b": (c.GetEndPoint(1).X, c.GetEndPoint(1).Y)}


def _ordenar(lazos):
    """Lazos de mayor a menor area: el primero es el exterior, el resto son huecos."""
    return sorted(lazos, key=lambda p: -abs(geom.area(p)))


def _curve_loop(pts, z, antihorario):
    if (geom.area(pts) > 0) != antihorario:
        pts = pts[::-1]
    p = [DB.XYZ(x, y, z - 1.0) for x, y in pts]
    lazo = DB.CurveLoop()
    for i in range(len(p)):
        lazo.Append(DB.Line.CreateBound(p[i], p[(i + 1) % len(p)]))
    return lazo


def _solido(pts, z, huecos=()):
    lazos = [_curve_loop(pts, z, True)] + [_curve_loop(h, z, False) for h in huecos]
    return DB.GeometryCreationUtilities.CreateExtrusionGeometry(lazos, DB.XYZ.BasisZ, 2.0)


def _contorno(solido):
    """(exterior, huecos) de la cara inferior de un solido; error si no es una sola pieza."""
    caras = [f for f in solido.Faces if isinstance(f, DB.PlanarFace) and f.FaceNormal.Z < -0.99]
    if len(caras) != 1:
        raise ValueError(u"El resultado no es una zona continua (%d piezas)." % len(caras))
    lazos = _ordenar([geom.fusionar_colineales(geom.quitar_cercanos(
        [(c.GetEndPoint(0).X, c.GetEndPoint(0).Y) for c in lz])) for lz in caras[0].GetEdgesAsCurveLoops()])
    return lazos[0], lazos[1:]


def _bool(a, b, tipo):
    return DB.BooleanOperationsUtils.ExecuteBooleanOperation(a, b, tipo)


def calcular(doc, la, lb, valle=False):
    """Nuevos contornos de ambas losas. No modifica el modelo."""
    A, B = _leer(doc, la), _leer(doc, lb)
    recta = geom.cruce(A["plano"], B["plano"])
    if recta is None:
        raise ValueError(u"Las dos losas tienen el mismo plano o son paralelas: no hay arista de cruce.")
    z = A["z"]
    sa, sb = _solido(A["pts"], z, A["huecos"]), _solido(B["pts"], z, B["huecos"])
    if _bool(sa, sb, DB.BooleanOperationsType.Intersect).Volume < 1e-6:
        raise ValueError(u"Las losas no se superponen.")
    union = _bool(sa, sb, DB.BooleanOperationsType.Union)
    cx = sum(p[0] for p in A["pts"] + B["pts"]) / len(A["pts"] + B["pts"])
    cy = sum(p[1] for p in A["pts"] + B["pts"]) / len(A["pts"] + B["pts"])
    lado_a = 1 if valle else -1          # g = zA - zB: A baja donde g<0
    nuevo = {}
    for datos, lado in ((A, lado_a), (B, -lado_a)):
        mitad = _solido(geom.semiplano(recta, lado, (cx, cy)), z)
        nuevo[datos["losa"].Id.IntegerValue] = _contorno(_bool(union, mitad, DB.BooleanOperationsType.Intersect))
    for d in (A, B):
        d["nuevo"], d["huecos_nuevo"] = nuevo[d["losa"].Id.IntegerValue]
        d["lin_nueva"] = _tramo_pendiente(d)
    return A, B


def _tramo_pendiente(d):
    """(lazo, indice) del tramo mas largo del contorno nuevo sobre la linea de pendiente original.

    El lazo 0 es el exterior; los demas, los huecos.
    """
    mejor = None
    for k, pts in enumerate([d["nuevo"]] + d["huecos_nuevo"]):
        for i in range(len(pts)):
            p, q = pts[i], pts[(i + 1) % len(pts)]
            if geom.subsegmento(p, q, d["a"], d["b"]):
                L = (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2
                if mejor is None or L > mejor[0]:
                    mejor = (L, (k, i))
    if mejor is None:
        raise ValueError(u"La linea de pendiente de la losa %d quedaria fuera del nuevo contorno."
                         % d["losa"].Id.IntegerValue)
    return mejor[1]


def _reescribir(doc, d):
    sc = DB.SketchEditScope(doc, u"Unir losas: contorno")
    sc.Start(d["sk"].Id)
    t = DB.Transaction(doc, u"Unir losas: lineas")
    t.Start()
    try:
        _crear_lineas(doc, d)
        t.Commit()
    except Exception:
        # Sin esto el scope queda abierto y el RollBack posterior tumba Revit
        if t.HasStarted():
            t.RollBack()
        sc.Cancel()
        raise
    sc.Commit(_Avisos())


def _crear_lineas(doc, d):
    sk, z = d["sk"], d["z"]
    viejos =[i for i in sk.GetAllElements() if isinstance(doc.GetElement(i), DB.ModelCurve)]
    vals = [(p, d["lin"].get_Parameter(p).AsInteger() if d["lin"].get_Parameter(p).StorageType == DB.StorageType.Integer
             else d["lin"].get_Parameter(p).AsDouble()) for p in _PARAMS_PENDIENTE]
    ab_x, ab_y = d["b"][0] - d["a"][0], d["b"][1] - d["a"][1]
    for i in viejos:
        doc.Delete(i)
    pend = None
    for k, pts in enumerate([d["nuevo"]] + d["huecos_nuevo"]):
        for i in range(len(pts)):
            p, q = pts[i], pts[(i + 1) % len(pts)]
            es_pend = ((k, i) == d["lin_nueva"])
            if es_pend and (q[0] - p[0]) * ab_x + (q[1] - p[1]) * ab_y < 0:
                p, q = q, p              # conserva el sentido original de la linea
            mc = doc.Create.NewModelCurve(DB.Line.CreateBound(DB.XYZ(p[0], p[1], z), DB.XYZ(q[0], q[1], z)), sk.SketchPlane)
            if es_pend:
                pend = mc.Id
    for bip, v in vals:
        doc.GetElement(pend).get_Parameter(bip).Set(v)   # se vuelve a leer: Revit puede regenerar la linea


def aplicar(doc, A, B):
    """Reescribe los dos contornos y verifica que los planos no cambiaron."""
    g = DB.TransactionGroup(doc, u"Unir losas en arista de cruce")
    g.Start()
    try:
        for d in (A, B):
            _reescribir(doc, d)
        for d in (A, B):
            nuevo = _plano_superior(doc, doc.GetElement(d["losa"].Id))
            if max(abs(x - y) for x, y in zip(nuevo, d["plano"])) > TOL_PLANO:
                raise ValueError(u"La pendiente de la losa %d cambio; se deshizo todo." % d["losa"].Id.IntegerValue)
    except Exception:
        g.RollBack()
        raise
    g.Assimilate()
