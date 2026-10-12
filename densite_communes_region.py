"""
Carte de densité des communes d'une région — script de traitement QGIS (version 1).

Version « couches locales » : l'utilisateur fournit sa couche de communes et sa
couche de population, choisit les champs de jointure et la région, et obtient
la carte de densité en trois classes avec les centroïdes des communes.

Né d'un exercice de PyQGIS (Master 2 Géomatique, Université Paris 8), réécrit
pour être robuste : surfaces calculées sur l'ellipsoïde, communes sans
population visibles, sorties Processing standard, compatible QGIS 3.34+ et 4.

Auteur : Mawa Diaw — Licence GPL v2 ou ultérieure
"""

import unicodedata

from qgis.core import (
    Qgis,
    QgsDistanceArea,
    QgsExpression,
    QgsFeature,
    QgsFeatureRequest,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsGraduatedSymbolRenderer,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingLayerPostProcessorInterface,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProject,
    QgsRendererRange,
    QgsRuleBasedRenderer,
    QgsSymbol,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QColor

try:  # QGIS >= 3.38 / QGIS 4
    from qgis.PyQt.QtCore import QMetaType

    T_DBL = QMetaType.Type.Double
except (ImportError, AttributeError):
    from qgis.PyQt.QtCore import QVariant

    T_DBL = QVariant.Double

FAST_INSERT = getattr(getattr(QgsFeatureSink, "Flag", QgsFeatureSink), "FastInsert")

# Noms de champ « région » reconnus pour remplir la liste déroulante
CHAMPS_REGION_CONNUS = ("region", "nom_reg", "nom_region", "reg_nom", "region_nom", "insee_reg", "code_reg")

# Seuils de l'énoncé : (min, max, couleur, libellé)
CLASSES = [
    (0, 100, "yellow", "Faible (< 100 hab/km²)"),
    (100, 200, "orange", "Moyenne (100 – 200 hab/km²)"),
    (200, 10 ** 7, "red", "Forte (> 200 hab/km²)"),
]
GRIS = QColor("#c8c8c8")
ROSE = QColor("#e8718d")

_POST_PROCESSEURS = []  # doivent survivre à l'instance de l'algorithme


def _cle_tri(texte):
    """Tri alphabétique qui ignore les accents (« Île-de-France » rangée au I)."""
    return unicodedata.normalize("NFD", texte).encode("ascii", "ignore").decode().lower()


def regions_du_projet():
    """Valeurs du champ région des couches de polygones ouvertes dans le projet."""
    valeurs = set()
    for couche in QgsProject.instance().mapLayers().values():
        if not isinstance(couche, QgsVectorLayer) or couche.geometryType() != Qgis.GeometryType.Polygon:
            continue
        for champ in couche.fields():
            if champ.name().lower() in CHAMPS_REGION_CONNUS:
                idx = couche.fields().indexOf(champ.name())
                valeurs.update(str(v) for v in couche.uniqueValues(idx) if v not in (None, ""))
    return sorted(valeurs, key=_cle_tri)


class _StyleDensite(QgsProcessingLayerPostProcessorInterface):
    def __init__(self, nom):
        super().__init__()
        self.nom = nom

    def postProcessLayer(self, layer, context, feedback):
        layer.setName(self.nom)
        plages = []
        for a, b, couleur, libelle in CLASSES:
            sym = QgsSymbol.defaultSymbol(Qgis.GeometryType.Polygon)
            sym.setColor(QColor(couleur))
            plages.append(QgsRendererRange(a, b, sym, libelle))
        racine = QgsRuleBasedRenderer.Rule(None)
        QgsRuleBasedRenderer.refineRuleRanges(racine, QgsGraduatedSymbolRenderer("densite", plages))
        gris = QgsSymbol.defaultSymbol(Qgis.GeometryType.Polygon)
        gris.setColor(GRIS)
        racine.appendChild(QgsRuleBasedRenderer.Rule(gris, label="Sans donnée", elseRule=True))
        layer.setRenderer(QgsRuleBasedRenderer(racine))
        layer.triggerRepaint()


class _StyleCentroides(QgsProcessingLayerPostProcessorInterface):
    def __init__(self, nom):
        super().__init__()
        self.nom = nom

    def postProcessLayer(self, layer, context, feedback):
        layer.setName(self.nom)
        sym = QgsSymbol.defaultSymbol(Qgis.GeometryType.Point)
        sym.setColor(ROSE)
        layer.renderer().setSymbol(sym)
        layer.triggerRepaint()


class DensiteCommunesRegion(QgsProcessingAlgorithm):
    COMMUNES = "COMMUNES"
    CHAMP_COMMUNES = "CHAMP_COMMUNES"
    CHAMP_REGION = "CHAMP_REGION"
    POPULATION = "POPULATION"
    CHAMP_POPULATION = "CHAMP_POPULATION"
    CHAMP_HABITANTS = "CHAMP_HABITANTS"
    REGION = "REGION"
    SORTIE = "SORTIE"
    CENTROIDES = "CENTROIDES"

    def __init__(self):
        super().__init__()
        self._regions = []

    def tr(self, texte):
        return QCoreApplication.translate("DensiteCommunesRegion", texte)

    def createInstance(self):
        return DensiteCommunesRegion()

    def name(self):
        return "densite_communes_region"

    def displayName(self):
        return self.tr("Carte de densité des communes d'une région")

    def group(self):
        return self.tr("Densité communale")

    def groupId(self):
        return "densite_communale"

    def shortHelpString(self):
        return self.tr(
            "<p>Produit la carte de densité de population des communes d'une région, "
            "à partir de deux couches que vous fournissez : les communes (polygones) et "
            "la population (couche ou table).</p>"
            "<p><b>Avant d'ouvrir l'outil</b>, chargez la couche des communes dans le projet : "
            "la liste des régions est lue dans son champ « region », « nom_reg » ou « INSEE_REG ».</p>"
            "<p>Classes : moins de 100 hab/km² en jaune, 100 à 200 en orange, plus de 200 en "
            "rouge ; communes sans population en gris. Les centroïdes sont ajoutés par-dessus.</p>"
        )

    def initAlgorithm(self, config=None):
        self._regions = regions_du_projet()
        options = self._regions or [self.tr("Aucune région trouvée : chargez la couche des communes puis rouvrez l'outil")]

        self.addParameter(QgsProcessingParameterFeatureSource(
            self.COMMUNES, self.tr("Couche des communes"), [QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterField(
            self.CHAMP_COMMUNES, self.tr("Champ de jointure des communes (code INSEE)"),
            defaultValue="insee", parentLayerParameterName=self.COMMUNES))
        self.addParameter(QgsProcessingParameterField(
            self.CHAMP_REGION, self.tr("Champ contenant la région"),
            defaultValue="region", parentLayerParameterName=self.COMMUNES))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.POPULATION, self.tr("Couche de population"), [QgsProcessing.TypeVector]))
        self.addParameter(QgsProcessingParameterField(
            self.CHAMP_POPULATION, self.tr("Champ de jointure de la population (code INSEE)"),
            defaultValue="cod_com", parentLayerParameterName=self.POPULATION))
        self.addParameter(QgsProcessingParameterField(
            self.CHAMP_HABITANTS, self.tr("Champ du nombre d'habitants"),
            defaultValue="ptot", parentLayerParameterName=self.POPULATION,
            type=QgsProcessingParameterField.Numeric))
        self.addParameter(QgsProcessingParameterEnum(
            self.REGION, self.tr("Région d'analyse"), options=options, defaultValue=0))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.SORTIE, self.tr("Carte de densité"), QgsProcessing.TypeVectorPolygon))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.CENTROIDES, self.tr("Centroïdes des communes"), QgsProcessing.TypeVectorPoint))

    def processAlgorithm(self, parametres, contexte, retour):
        if not self._regions:
            raise QgsProcessingException(self.tr(
                "Aucune région disponible : chargez la couche des communes dans le projet, "
                "puis rouvrez l'outil."))
        region = self._regions[self.parameterAsEnum(parametres, self.REGION, contexte)]

        communes = self.parameterAsSource(parametres, self.COMMUNES, contexte)
        population = self.parameterAsSource(parametres, self.POPULATION, contexte)
        if communes is None or population is None:
            raise QgsProcessingException(self.tr("Les couches en entrée sont invalides."))
        champ_com = self.parameterAsString(parametres, self.CHAMP_COMMUNES, contexte)
        champ_reg = self.parameterAsString(parametres, self.CHAMP_REGION, contexte)
        champ_pop = self.parameterAsString(parametres, self.CHAMP_POPULATION, contexte)
        champ_hab = self.parameterAsString(parametres, self.CHAMP_HABITANTS, contexte)
        retour.pushInfo(f"Région sélectionnée : {region}")

        # 1. Table de population indexée par code INSEE (équivalent de la jointure attributaire)
        habitants = {}
        for f in population.getFeatures(QgsFeatureRequest().setFlags(QgsFeatureRequest.Flag.NoGeometry)):
            code, nb = f[champ_pop], f[champ_hab]
            if code not in (None, "") and nb is not None:
                habitants[str(code)] = habitants.get(str(code), 0) + float(nb)
        retour.pushInfo(f"{len(habitants)} codes INSEE dans la couche de population.")
        retour.setProgress(20)

        # 2. Champs de sortie : ceux des communes + habitants, surface et densité
        champs = QgsFields(communes.fields())
        for nom in ("habitants", "surface_km2", "densite"):
            if champs.indexOf(nom) == -1:
                champs.append(QgsField(nom, T_DBL))
        sink, dest = self.parameterAsSink(parametres, self.SORTIE, contexte, champs,
                                          Qgis.WkbType.MultiPolygon, communes.sourceCrs())
        sink_pts, dest_pts = self.parameterAsSink(parametres, self.CENTROIDES, contexte, champs,
                                                  Qgis.WkbType.Point, communes.sourceCrs())
        if sink is None or sink_pts is None:
            raise QgsProcessingException(self.tr("Impossible de créer les couches de sortie."))

        # Surfaces mesurées sur l'ellipsoïde : justes quelle que soit la projection de la couche
        aire = QgsDistanceArea()
        aire.setSourceCrs(communes.sourceCrs(), contexte.transformContext())
        aire.setEllipsoid(contexte.ellipsoid() if contexte.ellipsoid() not in ("", "NONE") else "EPSG:7030")

        # 3. Communes de la région (équivalent de l'extraction par attribut)
        expression = f"{QgsExpression.quotedColumnRef(champ_reg)} = {QgsExpression.quotedValue(region)}"
        requete = QgsFeatureRequest().setFilterExpression(expression)
        total = communes.featureCount() or 1
        nb, sans_pop = 0, []
        for i, f in enumerate(communes.getFeatures(requete)):
            if retour.isCanceled():
                break
            geom = f.geometry()
            if geom is None or geom.isEmpty():
                continue
            if not geom.isMultipart():
                geom.convertToMultiType()
            km2 = aire.measureArea(geom) / 1e6
            hab = habitants.get(str(f[champ_com]))
            dens = round(hab / km2, 1) if hab is not None and km2 > 0 else None
            if dens is None:
                sans_pop.append(str(f[champ_com]))

            sortie = QgsFeature(champs)
            sortie.setGeometry(geom)
            attributs = list(f.attributes()) + [None] * (champs.count() - len(f.attributes()))
            attributs[champs.indexOf("habitants")] = hab
            attributs[champs.indexOf("surface_km2")] = round(km2, 3)
            attributs[champs.indexOf("densite")] = dens
            sortie.setAttributes(attributs)
            sink.addFeature(sortie, FAST_INSERT)

            # 4. Centroïde, garanti à l'intérieur de la commune
            point = QgsFeature(sortie)
            point.setGeometry(geom.pointOnSurface())
            sink_pts.addFeature(point, FAST_INSERT)
            nb += 1
            retour.setProgress(20 + 75 * i / total)

        if nb == 0:
            raise QgsProcessingException(self.tr(f"Aucune commune trouvée pour la région « {region} »."))
        retour.pushInfo(f"{nb} communes traitées.")
        if sans_pop:
            retour.pushWarning(f"{len(sans_pop)} commune(s) sans population, affichées en gris : "
                               + ", ".join(sans_pop[:15]) + (" …" if len(sans_pop) > 15 else ""))

        # 5. Nom, style et ordre des couches appliqués à leur chargement
        _POST_PROCESSEURS.clear()
        if contexte.willLoadLayerOnCompletion(dest):
            post = _StyleDensite(f"Densité — {region}")
            _POST_PROCESSEURS.append(post)
            contexte.layerToLoadOnCompletionDetails(dest).setPostProcessor(post)
        if contexte.willLoadLayerOnCompletion(dest_pts):
            post_pts = _StyleCentroides(f"Centroïdes — {region}")
            _POST_PROCESSEURS.append(post_pts)
            details = contexte.layerToLoadOnCompletionDetails(dest_pts)
            details.setPostProcessor(post_pts)
            if hasattr(details, "layerSortKey"):  # QGIS >= 3.32 : centroïdes au-dessus
                details.layerSortKey = 1
        return {self.SORTIE: dest, self.CENTROIDES: dest_pts}

