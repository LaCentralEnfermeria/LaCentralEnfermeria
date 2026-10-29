#!/usr/bin/env python3
"""
Revisión mensual de vigencia de normas y guías — La Central Enfermería (LACE)

Qué hace
  1. Lee el registro de verificación incrustado en el HTML del sitio
     (<script type="application/json" id="lace-verificacion">).
  2. Consulta la ficha de cada NOM en el Catálogo Mexicano de Normas (PLATIICA)
     y compara su "Estado de la Norma" con el que tiene el sitio.
  3. (Opcional) Si existe la variable ANTHROPIC_API_KEY, pregunta con búsqueda web
     si hay una edición más reciente de cada guía internacional registrada.
  4. Actualiza solo el sello de verificación (fechas y estado de las NOM) en el HTML.
  5. Escribe un informe en Markdown con lo que requiere revisión humana.

Qué NO hace (a propósito)
  No reescribe contenido clínico (dosis, algoritmos, tablas). Eso debe revisarlo
  una persona con criterio clínico antes de publicarse.

Uso
  python scripts/revisar_fuentes.py --html index.html --informe informe.md
  python scripts/revisar_fuentes.py --html index.html --dry-run      # no escribe el HTML
"""
import argparse
import datetime
import html as htmllib
import json
import os
import re
import sys
import time
import urllib.request

BLOQUE = re.compile(r'(<script type="application/json" id="lace-verificacion">)(.*?)(</script>)', re.S)
ESTADOS = r'(Vigente|Cancelad[ao]|Derogad[ao]|Modificad[ao]|Suspendid[ao]|Sustituid[ao])'
MODELO = os.environ.get('LACE_MODELO', 'claude-sonnet-5-5')
HERRAMIENTA_WEB = os.environ.get('LACE_WEB_SEARCH_TOOL', 'web_search_20250305')


def limpiar(htm: str) -> str:
    htm = re.sub(r'(?is)<(script|style).*?</\1>', ' ', htm)
    txt = re.sub(r'<[^>]+>', ' ', htm)
    return re.sub(r'\s+', ' ', htmllib.unescape(txt)).strip()


def obtener(url: str) -> str:
    """Descarga una página. Con LACE_OFFLINE_DIR lee archivos locales (para pruebas)."""
    carpeta = os.environ.get('LACE_OFFLINE_DIR')
    if carpeta:
        slug = url.rstrip('/').split('/')[-1]
        with open(os.path.join(carpeta, slug + '.html'), encoding='utf-8') as f:
            return f.read()
    ultimo = None
    for intento in range(3):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'LACE-revision-mensual/1.0'})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode('utf-8', 'replace')
        except Exception as e:  # red, 404, etc.
            ultimo = e
            time.sleep(2 * (intento + 1))
    raise RuntimeError(f'{type(ultimo).__name__}: {ultimo}')


def leer_estado(htm: str):
    """Devuelve (estado, fecha_dof) de una ficha de PLATIICA; None si no los encuentra."""
    txt = limpiar(htm)
    m = re.search(r'Estado de la Norma:\s*' + ESTADOS, txt, re.I)
    estado = m.group(1).capitalize() if m else None
    d = re.search(r'Fecha de publicación en el DOF:\s*(\d{1,2})/(\d{1,2})/(\d{4})', txt)
    dof = f'{int(d.group(1)):02d}/{int(d.group(2)):02d}/{d.group(3)}' if d else None
    return estado, dof


def revisar_normas(datos: dict, hoy: str):
    cambios, errores = [], []
    for n in datos.get('normas', []):
        clave = n.get('clave', '?')
        try:
            estado, dof = leer_estado(obtener(n['url']))
        except Exception as e:
            errores.append(f'{clave}: no se pudo consultar la ficha ({e})')
            continue
        if estado is None:
            errores.append(f'{clave}: la ficha no muestra «Estado de la Norma» (¿cambió el formato o el enlace?)')
            continue
        previo = n.get('estado', '')
        if re.search(r'pendiente', previo, re.I):
            cambios.append(f'{clave}: verificada por primera vez → **{estado}**')
        elif previo != estado:
            cambios.append(f'⚠️ **{clave}**: el estado cambió de «{previo}» a «{estado}». Revisar las secciones que la citan.')
        n['estado'] = estado
        n['verificado'] = hoy
        if dof and not n.get('dof'):
            n['dof'] = dof
    return cambios, errores


def extraer_json(texto: str):
    m = re.search(r'\{.*\}', texto or '', re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def revisar_guias(datos: dict, hoy: str):
    """Opcional: requiere ANTHROPIC_API_KEY. Solo SEÑALA posibles ediciones nuevas."""
    alertas, notas = [], []
    if not os.environ.get('ANTHROPIC_API_KEY'):
        notas.append('Revisión de guías internacionales omitida: no hay ANTHROPIC_API_KEY configurada.')
        return alertas, notas
    try:
        import anthropic
    except ImportError:
        notas.append('Revisión de guías omitida: falta instalar el paquete «anthropic».')
        return alertas, notas
    cliente = anthropic.Anthropic()
    for g in datos.get('guias', []):
        prompt = (
            f'Eres un verificador de guías clínicas. Hoy es {hoy}. Usando búsqueda web en fuentes oficiales, '
            f'determina si existe una edición o actualización de «{g["nombre"]}» publicada DESPUÉS de «{g["publicada"]}». '
            'Responde SOLO con JSON: {"nueva": true|false, "detalle": "una frase", "fuente": "URL oficial o vacío"}. '
            'Si no encuentras evidencia clara, responde nueva=false.'
        )
        try:
            resp = cliente.messages.create(
                model=MODELO, max_tokens=1500,
                tools=[{'type': HERRAMIENTA_WEB, 'name': 'web_search', 'max_uses': 4}],
                messages=[{'role': 'user', 'content': prompt}],
            )
            texto = ''.join(getattr(b, 'text', '') for b in resp.content if getattr(b, 'type', '') == 'text')
            r = extraer_json(texto)
        except Exception as e:
            notas.append(f'{g["nombre"]}: error al consultar ({type(e).__name__}).')
            continue
        if r is None:
            notas.append(f'{g["nombre"]}: respuesta no interpretable.')
        elif r.get('nueva'):
            alertas.append(f'📚 **{g["nombre"]}**: posible edición nueva — {r.get("detalle", "")} {r.get("fuente", "")}'.strip())
    return alertas, notas


def construir_informe(hoy, cambios, errores, alertas, notas, datos):
    L = [f'# Revisión mensual de vigencia — {hoy}', '']
    L.append('Este informe lo genera `scripts/revisar_fuentes.py`. **No se modificó ningún contenido clínico**; '
             'solo se actualizó el sello de verificación del sitio.')
    L += ['', '## Cambios en Normas Oficiales Mexicanas']
    L += [f'- {c}' for c in cambios] or ['- Sin cambios de estado.']
    L += ['', '## Posibles ediciones nuevas de guías internacionales']
    L += [f'- {a}' for a in alertas] or ['- Ninguna detectada.']
    if errores:
        L += ['', '## Consultas que fallaron (revisar a mano)'] + [f'- {e}' for e in errores]
    if notas:
        L += ['', '## Notas'] + [f'- {n}' for n in notas]
    L += ['', '## Secciones aún pendientes de contraste'] + [f'- {p}' for p in datos.get('pendientes', [])]
    L += ['', '**Siguiente paso:** una persona con criterio clínico debe revisar los puntos señalados y, '
              'si procede, actualizar el contenido y el «Registro de cambios».']
    return '\n'.join(L) + '\n'


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--html', default='index.html')
    ap.add_argument('--informe', default='informe.md')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()

    html = open(a.html, encoding='utf-8').read()
    m = BLOQUE.search(html)
    if not m:
        sys.exit('No se encontró el bloque lace-verificacion en el HTML.')
    datos = json.loads(m.group(2))
    hoy = datetime.date.today().isoformat()

    cambios, errores = revisar_normas(datos, hoy)
    alertas, notas = revisar_guias(datos, hoy)
    datos['generado'] = hoy

    nuevo = json.dumps(datos, ensure_ascii=False, indent=2)
    if not a.dry_run:
        html = BLOQUE.sub(lambda mm: mm.group(1) + '\n' + nuevo + '\n  ' + mm.group(3), html, count=1)
        open(a.html, 'w', encoding='utf-8').write(html)
    open(a.informe, 'w', encoding='utf-8').write(construir_informe(hoy, cambios, errores, alertas, notas, datos))

    hay = bool(cambios or errores or alertas)
    salida = os.environ.get('GITHUB_OUTPUT')
    if salida:
        with open(salida, 'a', encoding='utf-8') as f:
            f.write(f'hay_cambios={"true" if hay else "false"}\n')
    print(open(a.informe, encoding='utf-8').read())


if __name__ == '__main__':
    main()
