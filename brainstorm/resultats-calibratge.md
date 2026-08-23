# Resultats del calibratge — 2026-08-23

9 casos, mètodes reals de `src/calc/` de the target codebase, `deepseek-chat`,
`temperature=0`, esquema sintètic de l'espec.

## Veredicte

**La premissa aguanta.** DeepSeek fa la feina que el projecte li vol
encarregar, i la fa bé. Però el calibratge ha destapat **un bug de disseny
greu que no tenia res a veure amb el worker**, i ha refutat dues de les
meves pròpies afirmacions a la revisió crítica.

## Tanda final

```
json_ok        9/9      JSON vàlid amb out/why/diff_stat
offsets_ok     9/9      el bloc al fitxer és el que vam enviar
php_ok         9/9      el fitxer resultant passa php -l
sig_ok         9/9      signatura intacta
perimetre_ok   9/9      (vegeu la nota — no vol dir res)
indent_ok      8/9
identitat_ok   3/3      ← la pregunta que decidia el projecte
cap_perdua     3/3      cap línia original perduda a les tasques additives
fences         0/9      mai ha embolcallat en markdown
latència       mitjana 2.567 ms, rang 1.304–3.615 ms
tokens         in 6.190 (43% servits de cache) / out 2.979
```

---

## 🔴 Troballa 1 — Byte offsets vs índexs de caràcters

**La primera tanda va fallar 6 de 9 casos. Cap fallada era de DeepSeek.**

`token_get_all()` de PHP retorna **offsets en bytes**. Python els aplicava
a un `str`, que s'indexa **per caràcters**. Cada accent català abans del
símbol desplaça el tall:

```
extract.php diu:  start_byte=1965
ShiftCalc.php:  4.506 bytes  /  4.504 caràcters   ← 2 accents

slice sobre str    → 'blic function closeShift($shiftId) {'
slice sobre bytes  → 'public function closeShift($shiftId) {'
```

Dos caràcters menjats. En fitxers amb més accents abans del símbol, el
desplaçament arriba a esborrar la paraula `function` sencera — i llavors
ni tan sols la comprovació de signatura sabia què mirar.

**Per què és greu i no anecdòtic:**

- the target codebase té **comentaris en català a tot arreu**. No és un cas límit: és
  el cas normal. Pràcticament qualsevol fitxer del projecte el dispara.
- És **silenciós**. El codi no peta: retalla el principi del bloc i n'afegeix
  bytes del final. El resultat sovint segueix semblant codi.
- Passa **abans** de qualsevol crida al worker. Cap prompt, cap model i cap
  reintent el pot arreglar.

**Regla per al servidor MCP: tot el camí extracció → payload → splice →
escriptura treballa en `bytes`.** Es descodifica a `str` només per enviar-ho
al worker i es torna a codificar en rebre. Qualsevol `read_text()` en aquest
camí és un bug esperant fitxer amb accents.

**I confirma RF-1 empíricament**, millor del que ho hauria pogut fer cap
argument: a la primera tanda, `perimetre_ok` va donar **9/9 mentre 3 fitxers
quedaven sintàcticament trencats**. La porta va dir "perfecte" sobre codi
destruït. És exactament el que RF-1 predeia, observat.

I la porta que sí ho detecta —`offsets_ok`, el guard de §4.3— és la que vaig
afegir després d'escriure RF-1. Ara està validada amb dades: la primera
tanda hauria donat `offsets_ok` fals als 9 casos.

---

## 🟢 Troballa 2 — La pregunta que decidia el projecte: resposta afirmativa

> Quan se li demana explícitament, DeepSeek sap tornar un bloc de codi real
> de the target codebase byte a byte intacte?

**Sí. 3/3, byte a byte, sense una diferència.**

També, en les tasques additives, **3/3 sense perdre cap línia original** —
el mode de fallada que RF-2 identificava com el risc real no s'ha
manifestat en aquesta mostra. Això no el descarta (9 casos són pocs i eren
mètodes de 5-28 línies), però treu urgència a la porta de substància:
passa de "imprescindible al dia 1" a "necessària abans de l'ús massiu".

Altres observacions:
- **0/9 amb tanques markdown.** La regla del prompt de sistema funciona.
- **9/9 amb `why` i `diff_stat`.** L'esquema sintètic es respecta sencer.
- **9/9 JSON vàlid.** `response_format=json_object` és fiable.

---

## 🟡 Troballa 3 — La indentació sí que s'escapa (1/9)

`ShiftCalc::createShift`, tasca `phpdoc`: tot correcte —compila, cap
pèrdua, signatura intacta— excepte que el bloc retornat no comença amb la
mateixa indentació que l'original.

És cosmètic, però en un projecte de 35.000 línies el cosmètic acumulat és
deute. I val la pena notar que és **el mateix problema que §11 preveia per
a Python**, aparegut en PHP.

**Mitigació confirmada com a encertada:** normalitzar la indentació del
bloc a 0 abans d'enviar-lo i re-aplicar-la en rebre. Així el worker no ha
d'encertar-la mai. La decisió de mantenir el camp `indent` al payload des
del dia 1 queda justificada amb dades, no per precaució.

---

## ❌ Refutació 1 — RF-5 era fals: la latència no és un problema

Vaig escriure: "5-30s per crida, Bifrost és més lent que fer-ho
directament, per tant ha de ser una eina de lot".

**Mesurat: mitjana 2,6s, màxim 3,6s.** És pràcticament el mateix que triga
Claude a fer un `Edit`. L'argument de latència contra l'ús interactiu
**no s'aguanta** i s'ha de retirar.

L'argument de RF-4 (l'estalvi de context només és gran en volum) segueix
sent vàlid i segueix recomanant el lot. Però ara és per economia, no per
velocitat — i això és més fluix. Una eina que no penalitza en temps pot
usar-se també en interactiu sense cost.

---

## ❌ Refutació 2 — RF-10 era massa dur amb el prompt caching

Vaig escriure que el caching seria "marginal" perquè només cobreix el prompt
de sistema.

**Mesurat: 2.688 de 6.190 tokens d'entrada servits de cache — el 43%.**

Molt més del que preveia. El prefix cachejable inclou més del que pensava, i
en un lot on el prompt de sistema es repeteix N vegades l'estalvi és real.
No canvia cap decisió d'arquitectura, però la frase "no dissenyeu res
assumint-ne guany" era excessiva.

---

## Què s'ha de fer diferent al pla

| # | Canvi | Origen |
|---|---|---|
| 1 | **Tot el camí de dades en `bytes`.** Prohibit `read_text()` a extracció/splice. | Troballa 1 |
| 2 | `offsets_ok` (guard de §4.3) és **la** porta crítica, no una millora. | Troballa 1 |
| 3 | Retirar la porta de perímetre i el test que la verifica. | Troballa 1, confirma RF-1 |
| 4 | Normalitzar indentació a 0 al payload; camp `indent` obligatori. | Troballa 3 |
| 5 | Retirar RF-5. La latència no és un argument. | Refutació 1 |
| 6 | Suavitzar RF-10. El caching aporta ~43%. | Refutació 2 |
| 7 | Porta de substància: baixa de "dia 1" a "abans de l'ús massiu". | Troballa 2 |

---

## Cost

9 crides, 6.190 tokens d'entrada i 2.979 de sortida. Cèntims.

La tanda que va destapar el bug de bytes va costar el mateix, i hauria estat
un dia sencer de depuració confosa si el bug s'hagués trobat amb el servidor
MCP ja escrit i patchejant fitxers de producció.

## Reproduir

```bash
export DEEPSEEK_API_KEY=...
python3 calibratge/calibra.py --casos 9
```
