"""Check a Power BI report theme against the keys of reportThemeSchema-2.159.json.

The key lists below were read from microsoft/powerbi-desktop-samples ("Report Theme JSON Schema").
Usage:  python check_powerbi_theme.py brand/themes/powerbi/harborline-light.json [more.json ...]
"""
import json
import re
import sys

TOP = {"$schema", "name", "baseTheme", "visualStyles", "dataColors", "icons", "textClasses", "foreground", "firstLevelElements", "secondLevelElements", "thirdLevelElements", "fourthLevelElements", "background", "secondaryBackground", "good", "neutral", "bad", "maximum", "center", "minimum", "null", "accent", "tableAccent", "foregroundLight", "foregroundDark", "foregroundNeutralLight", "foregroundNeutralDark", "foregroundNeutralSecondary", "foregroundNeutralSecondaryAlt", "foregroundNeutralSecondaryAlt2", "foregroundNeutralTertiary", "foregroundNeutralTertiaryAlt", "foregroundSelected", "foregroundButton", "backgroundLight", "backgroundNeutral", "backgroundDark", "hyperlink", "visitedHyperlink", "shapeStroke", "disabledText", "mapPushpin"}
TEXTCLASSES = {"title", "label", "callout", "header", "largeTitle", "dataTitle", "boldLabel", "largeLabel", "largeLightLabel", "lightLabel", "semiboldLabel", "smallLabel", "smallLightLabel", "smallDataLabel"}
TEXTPROPS = {"fontFace", "fontSize", "fontWeight", "color"}
COMMON = {
    "title": "alignment background bold fontColor fontFamily fontSize heading italic show text titleWrap underline",
    "subTitle": "alignment bold fontColor fontFamily fontSize heading italic show text titleWrap underline",
    "background": "color show transparency",
    "border": "color radius show width",
    "padding": "bottom left right top",
    "dropShadow": "angle color position preset shadowBlur shadowDistance shadowSpread show transparency",
    "visualHeader": "background border foreground show transparency showCommentButton showCopyVisualImageButton showDrillDownExpandButton "
                    "showDrillDownLevelButton showDrillRoleSelector showDrillToggleButton showDrillUpButton showFilterRestatementButton "
                    "showFocusModeButton showOptionsMenu showPinButton showSeeDataLayoutToggleButton showSmartNarrativeButton showTooltipButton "
                    "showVisualErrorButton showVisualInformationButton showVisualWarningButton",
    "visualTooltip": "actionFontColor background themedBackground themedTitleFontColor themedValueFontColor titleFontColor valueFontColor bold "
                     "fontFamily fontSize italic section sentenceTemplate show showActionsInTooltips showChartSpecificTooltips showSentenceFormat "
                     "showTooltipFieldsOnly showValuesInBold transparency type underline",
    "general": "allowBinnedLineSample allowOverlappingPointsSample keepLayerOrder altText height width x y",
    "spacing": "customizeSpacing spaceAboveDivider spaceAbovePlotArea spaceAboveSubtitle spaceBelowSubTitle spaceBelowTitle spaceBelowTitleArea verticalSpacing",
    "divider": "color ignorePadding show style width",
}
VISUAL = {
    "page": {"background": "color image transparency", "outspace": "color image transparency",
             "outspacePane": "backgroundColor border borderColor checkboxAndApplyColor fontFamily foregroundColor headerSize inputBoxColor searchTextSize titleSize transparency width",
             "filterCard": "$id backgroundColor border borderColor fontFamily foregroundColor inputBoxColor textSize transparency",
             "displayArea": "verticalAlignment"},
    "cardVisual": {"value": "$id bold customFormatString fontColor fontFamily fontSize horizontalAlignment italic labelDisplayUnits labelPrecision show showBlankAs textWrap transparency underline",
                   "label": "$id alignBaselines bold fontColor fontFamily fontSize heading horizontalAlignment italic matchValueAlignment position show text textWrap transparency underline",
                   "accentBar": "$id color position show transparency width",
                   "padding": "$id bottomMargin leftMargin paddingIndividual paddingSelection paddingUniform rightMargin topMargin"},
    "card": {"labels": "bold color fontFamily fontSize italic labelDisplayUnits labelPrecision preserveWhitespace underline",
             "categoryLabels": "bold color fontFamily fontSize italic preserveWhitespace show underline", "wordWrap": "show", "general": "formatString"},
    "kpi": {"indicator": "bold fontColor fontFamily fontSize horizontalAlignment iconSize indicatorDisplayUnits indicatorPrecision italic showIcon underline verticalAlignment",
            "goals": "bold direction distanceFontColor distanceFontFamily distanceLabel fontSize goalFontColor goalFontFamily goalText italic labelPrecision showDistance showGoal titleBold titleFontSize titleItalic titleUnderline underline",
            "status": "badColor direction goodColor neutralColor", "trendline": "show transparency",
            "lastDate": "bold fontSize italic lastDateFontColor lastDateFontFamily show underline"},
    "tableEx": {"grid": "gridHorizontal gridHorizontalColor gridHorizontalWeight gridVertical gridVerticalColor gridVerticalWeight imageHeight imageWidth outlineColor outlineStyle outlineWeight rowPadding textSize",
                "columnHeaders": "alignment autoSizeColumnWidth backColor bold columnAdjustment customColumnWidth defaultColumnWidth fontColor fontFamily fontSize italic outlineColor outlineStyle outlineWeight underline wordWrap",
                "values": "backColor backColorPrimary backColorSecondary bold fontColor fontColorPrimary fontColorSecondary fontFamily fontSize icon italic outlineColor outlineStyle outlineWeight underline urlIcon webURL wordWrap",
                "total": "backColor bold fontColor fontFamily fontSize italic label outlineColor outlineStyle outlineWeight totals underline"},
    "pivotTable": {"grid": "gridHorizontal gridHorizontalColor gridHorizontalWeight gridVertical gridVerticalColor gridVerticalWeight imageHeight imageWidth outlineColor outlineStyle outlineWeight rowPadding textSize",
                   "columnHeaders": "alignment autoExpand backColor bold fontColor fontFamily fontSize outlineColor outlineStyle outlineWeight showExpandCollapseButtons titleAlignment wordWrap",
                   "rowHeaders": "alignment autoExpand backColor bold expandCollapseButtonsColor expandCollapseButtonsSize expandCompositeHierarchy fontColor fontFamily fontSize italic legacyStyleDisabled outlineColor outlineStyle outlineWeight repeatRowHeaders showExpandCollapseButtons stepped steppedLayoutIndentation underline unfrozen urlIcon wordWrap",
                   "values": "backColor backColorPrimary backColorSecondary bandedRowHeaders bold fontColor fontColorPrimary fontColorSecondary fontFamily fontSize icon italic outlineColor outlineStyle outlineWeight underline urlIcon valuesOnRow webURL wordWrap",
                   "subTotals": "$id applyToHeaders backColor bold columnSubtotals columnSubtotalsLabel fontColor fontFamily fontSize italic levelSubtotalEnabled levelSubtotalLabel perColumnLevel perRowLevel rowSubtotals rowSubtotalsLabel rowSubtotalsPosition underline",
                   "total": "applyToHeaders backColor bold fontColor fontFamily fontSize italic underline"},
    "slicer": {"header": "background bold fontColor fontFamily italic outlineStyle show showRestatement text textSize underline",
               "items": "accessibilityContrastProperties background bold expandCollapseIconColor expandCollapseToggleType fontColor fontFamily italic outlineStyle padding steppedLayoutIndentation textSize underline",
               "selection": "selectAllCheckboxEnabled singleDate singleSelect strictSingleSelect",
               "slider": "color handleBorderColor handleFillColor handleHeight handleSize handleWidth secondaryLineColor show",
               "date": "background bold fontColor fontFamily hideDatePickerButton italic textSize underline"},
}
HEX = re.compile(r"^#[0-9a-fA-F]{8}$|^#(?:[0-9a-fA-F]{3}){1,2}$")


def check(path):
    errs = []
    with open(path, encoding="utf-8") as fh:
        t = json.load(fh)  # raises on invalid JSON
    for k, v in t.items():
        if k not in TOP: errs.append(f"top-level key not in schema: {k}")
        elif k not in {"$schema", "name", "visualStyles", "dataColors", "textClasses", "icons", "baseTheme"} and not HEX.match(v):
            errs.append(f"{k}: not a hex colour: {v}")
    for c in t.get("dataColors", []):
        if not HEX.match(c): errs.append(f"dataColors: bad hex {c}")
    for name, tc in t.get("textClasses", {}).items():
        if name not in TEXTCLASSES: errs.append(f"textClass not in schema: {name}")
        errs += [f"textClasses.{name}.{p} not allowed" for p in tc if p not in TEXTPROPS]
        if not 8 <= tc.get("fontSize", 10) <= 60: errs.append(f"textClasses.{name}.fontSize out of 8..60")
    for vis, styles in t.get("visualStyles", {}).items():
        for cards in styles.values():
            allowed = {} if vis == "*" else VISUAL.get(vis)
            if allowed is None: errs.append(f"visual not covered by this checker: {vis}"); continue
            for card, items in cards.items():
                props = COMMON.get(card) if vis in ("*",) or card in COMMON and vis != "page" else None
                if vis != "*" and card in allowed: props = allowed[card]
                if props is None: errs.append(f"{vis}.{card}: card not in schema list"); continue
                pset = set(props.split())
                if not isinstance(items, list): errs.append(f"{vis}.{card}: must be an array"); continue
                for it in items:
                    for p, v in it.items():
                        if p not in pset: errs.append(f"{vis}.{card}.{p}: property not in schema list")
                        if isinstance(v, dict) and (set(v) != {"solid"} or not HEX.match(v["solid"].get("color", ""))):
                            errs.append(f"{vis}.{card}.{p}: fill must be {{solid:{{color:#hex}}}}")
                        if p in ("fontSize",) and not 8 <= v <= 60: errs.append(f"{vis}.{card}.{p}: fontSize out of 8..60")
    return errs


ok = True
for p in sys.argv[1:]:
    e = check(p)
    print(("PASS " if not e else "FAIL ") + p)
    for x in e: print("   ", x)
    ok &= not e
sys.exit(0 if ok else 1)
