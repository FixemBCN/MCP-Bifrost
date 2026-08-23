# Revisió crítica — ulls frescos

**Data:** 2026-08-23 · sobre `pla.md` v0.4
📊 **Contrastat amb dades:** [`../calibratge/RESULTATS.md`](../calibratge/RESULTATS.md).
RF-1 confirmat empíricament; **RF-5 refutat** (latència 2,6s, no 5-30s);
**RF-10 suavitzat** (43% de cache, no marginal); RF-2 sense manifestar-se
en 9 casos.
**Premissa de la revisió:** arribo de fora, no he participat en cap decisió
prèvia, i la meva feina és trobar per què això fallarà. No dono res per bo
perquè estigui escrit al pla.

**Veredicte curt:** el projecte és construïble i té un cas d'ús real, però
**el pla justifica el projecte amb l'argument equivocat** i **tres de les
seves garanties de seguretat no garanteixen res**. Cap dels dos problemes
és fatal, però tots dos canvien què s'ha de construir.

---

## 🔴 RF-1 — La porta de perímetre no pot fallar mai

**El pla diu** (§4.1, porta 3): "comparació byte a byte de tot el fitxer
fora del rang objectiu. Ha de ser idèntic. Aquesta és la garantia del 100%
de preservació."

### Per què no pot fallar

El MCP no rep un fitxer del worker. Rep **un bloc**, i el fitxer nou el
construeix ell mateix:

```python
nou = original[:start] + bloc_del_worker + original[end:]
#     └──────┬──────┘                      └──────┬──────┘
#      perímetre esquerre               perímetre dret
#      copiat literalment               copiat literalment
```

La porta comprova després això:

```python
assert nou[:start] == original[:start]          # sempre cert
assert nou[-(len(original)-end):] == original[end:]  # sempre cert
```

Les dues bandes del perímetre són **les mateixes cadenes d'objecte** que
acabem de concatenar. Comparar-les és comparar `x` amb `x`. No existeix cap
entrada del worker — ni codi trencat, ni resposta buida, ni 50.000 línies
d'escombraries — que pugui fer fallar aquesta comprovació, perquè el
worker **no té cap manera d'influir sobre els bytes de fora del bloc**.

Val la pena veure-ho des de l'altra banda: perquè la porta fallés, hauria
de passar que `original[:start] != original[:start]`. Això no és improbable,
és impossible.

### D'on ve l'error

La porta té sentit — però **en una altra arquitectura**. L'espec original
va néixer pensant en el patró habitual "el model reescriu el fitxer i
nosaltres el desem":

```
worker → fitxer sencer → MCP el desa
```

Aquí sí que el worker toca el perímetre, i comparar-lo és **la** defensa
essencial: és l'única manera de detectar que el model s'ha inventat una
funció al mig, ha reordenat imports o s'ha menjat el final del fitxer.

Però nosaltres vam triar l'altre patró:

```
worker → només el bloc → MCP fa el splice
```

I aquest patró **elimina l'amenaça estructuralment**. La preservació del
perímetre deixa de ser una cosa que es verifica i passa a ser una cosa que
es *garanteix pel disseny*. La porta és una defensa heretada contra un
atac que la nostra arquitectura ja va fer impossible.

Això no és dolent — **és el senyal que vam triar bé l'arquitectura**. El
problema és només que el pla es queda el mèrit dues vegades: primer pel
disseny, i després com si fos una validació addicional.

### Per què és perillós deixar-la escrita

1. **Confiança falsa.** El pla presenta aquesta porta com "la garantia del
   100% de preservació que promet l'espec". Qui llegeixi el pla en
   deduirà que hi ha una comprovació activa protegint el fitxer. No n'hi
   ha cap: hi ha una propietat del `+`.
2. **Un test verd que no prova res.** El pla demana (§7, Fase 0) "test que
   verifiqui byte a byte la preservació del perímetre". Aquest test passarà
   sempre, també amb el codi trencat. Un test que no pot fallar mai és
   pitjor que cap test: dona senyal positiu sense mirar res.
3. **Desvia l'atenció.** Mentre es mira el perímetre, ningú mira dins del
   bloc, que és l'únic lloc on el worker pot fer mal (→ RF-2).

### Què sí que val la pena comprovar

Hi ha **una** comprovació d'aquesta família que no és tautològica, i és
important. No es refereix al perímetre sinó a **els offsets**:

```python
assert original[start:end] == src_que_vam_enviar
```

Això **sí que pot fallar**, i per motius reals:

- El fitxer ha canviat entre l'extracció i l'escriptura (un patch anterior
  del mateix lot, un altre procés, l'editor de l'usuari).
- `extract.php` ha calculat malament els límits del símbol.
- S'ha barrejat una operació d'un altre fitxer.

I quan falla, el dany que evita és total: fer el splice amb offsets
equivocats **injecta el bloc al mig d'un altre mètode** i destrueix codi
sense que `php -l` ho noti necessàriament.

Aquesta comprovació ja existeix al pla: és el guard de rang estancat
(§4.3). **La conclusió, doncs, no és "falta una porta" sinó "la porta útil
d'aquesta família ja la tenim, i la que hi ha a §4.1 sobra".**

### Nota sobre la porta 2 (integritat estructural)

Té el mateix defecte, per la mateixa raó. El pla la justifica així: "atrapa
el cas clàssic del worker que s'emporta codi veí".

Amb extracció per símbol, **el worker no veu mai codi veí**. Rep un únic
mètode aïllat. No pot esborrar el mètode del costat perquè no sap que
existeix, igual que no pot esborrar un fitxer que no li hem enviat.

L'única part que conserva valor és la inversa: comprovar que el bloc
retornat **no contingui més d'una definició de símbol** (que el worker no
hagi afegit una funció germana pel seu compte). Això sí que és possible i
sí que cal mirar-ho — però és una comprovació sobre el **bloc**, de dues
línies amb `extract.php`, no una comparació de tot el fitxer.

### Resum

| Comprovació | Pot fallar? | Val la pena? |
|---|---|---|
| Perímetre idèntic fora del rang | ❌ Mai | No. Eliminar. |
| Símbols del fitxer sencer abans/després | ❌ Pràcticament mai | No. Eliminar. |
| El bloc conté exactament 1 símbol | ✅ Sí | **Sí**, i és barat. |
| `original[start:end]` == `src` enviat | ✅ Sí | **Sí**, i és crític. Ja és §4.3. |
| `php -l` sobre el fitxer resultant | ✅ Sí | **Sí.** Única porta real del pla. |
| El bloc no ha perdut substància | ✅ Sí | **Sí** → RF-2, i no existeix encara. |

---

## 🔴 RF-2 — El risc real no té cap porta

Si el worker no pot tocar el que hi ha fora del bloc, què pot fer malament?

1. Tornar codi que no compila → **`php -l` ho atrapa.** ✅
2. Tornar codi que compila però **ha perdut lògica de dins del mètode** —
   ha eliminat una branca `if`, s'ha deixat una crida, ha simplificat un
   `try/catch`. → **Cap porta ho atrapa.** ❌
3. Tornar codi correcte però amb semàntica canviada → cap porta. ❌

El cas 2 és el mode de fallada més probable d'un model barat amb context
reduït, i és exactament el que el pla no cobreix.

**Proposta: porta de substància.** Abans i després, extreure del bloc el
conjunt de:
- crides a funcions/mètodes (`->foo(`, `Foo::bar(`, `bar(`)
- noms de variables
- paraules clau de control (`if`, `foreach`, `try`, `throw`, `return`)

Si el bloc nou n'ha **perdut** algun que la instrucció no demanava
eliminar, es rebutja i s'informa Claude de què ha desaparegut. És barat
(regex sobre el bloc, o millor: `token_get_all` que ja tenim) i ataca el
risc real en lloc del risc imaginari.

Això substitueix les portes 2 i 3, no s'hi suma.

---

## 🔴 RF-3 — No hi ha xarxa semàntica sota tot això

El pla delega la validació semàntica a "Claude executa el test suite"
(§3 del workflow original). He mirat què hi ha:

| | |
|---|---|
| Codi PHP propi | ~34.900 línies |
| Fitxers de test | 15 |
| Línies de test | 1.845 |
| Framework | **Cap** (scripts ad-hoc, no PHPUnit) |
| `composer.json` amb dev-dependencies | **No** |

**La ràtio és de ~1 línia de test per cada 19 de codi, sense runner.** No
hi ha cap manera d'executar-ho tot i obtenir un verd/vermell fiable.

Això vol dir que l'única barrera contra una regressió semàntica introduïda
per un model barat és **que algú se n'adoni fent servir l'aplicació**. En
codi que gestiona factures, pagaments a contractistes i fitxatges.

**Aquesta és, de lluny, la troballa més greu de la revisió**, i no és un
problema de MCP-Bifrost: és un problema de the target codebase que MCP-Bifrost
**amplifica**. Automatitzar canvis massius sobre una base sense xarxa de
proves multiplica la superfície de risc a la mateixa velocitat que
multiplica la productivitat.

**Recomanació dura:** no desplegar Bifrost en mode massiu sobre lògica de
negoci (`Calc/`, `App/`, `api/`) fins que hi hagi una xarxa mínima. No cal
cobertura completa — cal un *smoke test* executable que carregui les
classes, cridi els camins principals i falli fort. Es pot construir
*amb* Bifrost, sobre codi de baix risc, com a primera feina real del
projecte. És la manera d'obtenir valor immediat sense apostar el negoci.

---

## 🟠 RF-4 — El pla ven estalvi de context, i l'estalvi és petit

**El pla diu**: reducció del 85-95% (l'espec) o ~99% (§2.5). Aquests
números comparen el bloc extret amb **el fitxer sencer**. Però ningú
proposava enviar el fitxer sencer. La comparació honesta és contra **què
gastaria Claude fent l'edició ell mateix**, que amb l'eina `Edit` és
`old_string` + `new_string`, no el fitxer.

Fem els números per un mètode de 30 línies (~400 tokens):

| Camí | Context de Claude consumit |
|---|---|
| Claude edita directament | llegir ~400 + escriure ~400 ≈ **800 tok** |
| Via Bifrost | instrucció ~60 + resposta ~15 ≈ **75 tok** |

Estalvi real: ~725 tokens per edició. **Cert, però modest** — i amb una
trampa: si Claude ha hagut de **llegir el mètode** per saber què demanar,
ja ha pagat els 400 tokens de lectura i l'estalvi cau a la meitat.

**On l'argument sí que aguanta, i molt:**

Quan Claude **no necessita llegir el codi** per formular la instrucció.
"Afegeix PHPDoc als 202 mètodes d'AdminService", "canvia totes les crides a
l'API antiga", "afegeix logging d'errors a cada mètode de `Calc/`".

| | Claude ho fa tot | Via Bifrost |
|---|---|---|
| 202 mètodes × ~800 tok | **~161.000 tok** — no hi cap en una finestra | 202 × ~75 = **~15.000 tok** |

**Aquí sí que hi ha un 90% real, i a més fa possible una cosa que
literalment no cabia abans.**

**Conseqüència per al projecte:** la joia de la corona de Bifrost **no és
"arregla aquest bug"** — per un bug puntual Claude ha de llegir el codi de
totes maneres, i l'estalvi no compensa la latència ni el risc. La joia és
**la transformació mecànica en volum**, on Claude no llegeix res i el
guany és d'ordre de magnitud.

El pla hauria de dir això obertament, perquè canvia les prioritats: el
tool més valuós no és `fix_symbol` d'un en un, sinó **`fix_symbols` en
lot** amb una sola instrucció aplicada a N símbols.

---

## 🟠 RF-5 — ❌ REFUTAT — En interactiu, Bifrost és més lent que no usar-lo

> **Mesurat el 2026-08-23: mitjana 2,6s, màxim 3,6s.** Pràcticament igual
> que un `Edit` de Claude. Aquesta troballa no s'aguanta i es retira. La
> recomanació de treballar en lot segueix vàlida, però per economia (RF-4),
> no per velocitat.

Una crida a DeepSeek amb un bloc de codi són segons — típicament 5-30s
segons mida i càrrega. Claude fent l'`Edit` directament: ~2s.

Per una correcció puntual, **l'usuari espera més i assumeix més risc a
canvi d'estalviar uns centenars de tokens**. És un mal negoci.

Reforça RF-4: Bifrost ha de ser una eina de **lot**, no d'interacció. I el
pla hauria de fer explícit el criteri perquè Claude no la faci servir quan
no toca. Sense aquest criteri escrit, Claude tendirà a usar l'eina perquè
existeix.

**Criteri proposat, a escriure a la descripció del tool:**
> Usa Bifrost quan tinguis **≥5 transformacions similars** o quan la
> instrucció es pugui formular **sense haver llegit el codi**. Per una
> correcció puntual que ja tens al davant, edita-la tu.

---

## 🟠 RF-6 — Claude queda cec després del primer patch

El MCP retorna `OK` + `diff_stat`. Claude **no veu el codi resultant**.

En un lot això s'acumula: després de 10 patches sobre `AdminService.php`, el
model mental que Claude té del fitxer correspon a l'estat **inicial**. Si
la feina 11 depèn del que va fer la 3a, decidirà sobre informació falsa —
i ho farà amb total confiança, perquè totes les respostes van ser `OK`.

És l'efecte secundari directe de l'objectiu "0 tokens al context de
Claude": la ceguesa **és** l'estalvi.

**Mitigació proposada:** que el retorn inclogui un **diff unificat compacte**
(no el bloc sencer) quan el canvi supera un llindar, i només `OK` per sota.
Un diff de 3 línies costa ~40 tokens i manté Claude sincronitzat. La
decisió "0 tokens sempre" és massa absoluta; el que es vol és "pocs
tokens", que no és el mateix.

---

## 🟡 RF-7 — L'adreçament per símbol assumeix que els símbols són petits

Mesurat a the target codebase:

- `AdminService.php`: 200 símbols en 4.653 línies → mitjana ~23 línies. ✅
- `Database.php`: **5 símbols en 1.509 línies**. `runMigrations` fa
  **1.380 línies ella sola**.

Per `runMigrations`, `fix_symbol` no extreu una unitat manejable: extreu
gairebé tot el fitxer. Zero estalvi, i un bloc massa gran perquè un model
barat el retorni sencer sense perdre-hi res (que és exactament RF-2).

**Cal un límit dur**: si el símbol supera N línies (proposta: 150), el tool
**rebutja** i diu a Claude que faci servir `fix_range` o que ho faci ell.
Fallar clarament és molt millor que degradar-se en silenci.

---

## 🟡 RF-8 — El paral·lelisme no serveix per al cas principal

El pla proposa processar lots en paral·lel amb **serialització per fitxer**
(§4.6). Però el target principal és `AdminService.php`: **un sol fitxer amb 200
símbols**. Un lot de 30 correccions hi cauria tot al mateix fitxer i, amb
serialització per fitxer, s'executaria **completament en sèrie**.

30 crides × 15s = **7 minuts i mig** per un lot. El paral·lelisme, tal com
està dissenyat, no s'activa mai on més falta fa.

**Alternativa:** serialitzar l'**escriptura**, no la **crida**. Les crides
al worker són independents (cada símbol es processa aïllat); només l'aplicació
al disc necessita ordre. Llançar les N crides en paral·lel, recollir-ne els
resultats, validar-los tots i aplicar-los en sèrie sobre offsets recalculats.
Passa de 7 minuts a menys d'un.

Això encaixa de manera natural amb `patch_group` (§4.4) — i de fet suggereix
que **`patch_group` no és una fase 2, és l'arquitectura correcta des del dia
1**: el mode natural de Bifrost és el lot, no la crida solta.

---

## 🟡 RF-9 — `ctx` amb signatures nues probablement no serveix de res

L'espec envia `ctx: ["dep_signature_1", ...]`. Per la tasca "afegeix una
guarda si `$data` és buit", saber que existeix `ClientCalc::getClients()`
no ajuda gens. El worker necessita saber **què conté `$data`**, i això no
és una signatura.

És barat d'enviar, i per això ningú se n'adonarà que no serveix. Però
enviar context inútil és pitjor que no enviar-ne: consumeix tokens i pot
distreure el model.

**Cal que el calibratge ho mesuri**: fer la mateixa tasca amb `ctx` i
sense, i comparar. Si no hi ha diferència, fora del schema.

---

## 🟡 RF-10 — ⚠️ SUAVITZAT — El prompt caching aporta menys del que insinua l'espec

> **Mesurat: 2.688 de 6.190 tokens d'entrada servits de cache (43%).** Molt
> més del que aquesta secció preveia. No canvia cap decisió, però la frase
> "no dissenyeu res assumint-ne guany" era excessiva.

L'espec destaca "leveraging DeepSeek Prompt Caching" com a optimització.
El caching actua sobre el **prefix comú**, que aquí és el prompt de sistema:
~250 tokens. La part variable (`src`) és la que domina i **no es cacheja
mai**, perquè canvia a cada crida.

No és fals, però és marginal. No hauria de figurar com a pilar
d'optimització; convé no dissenyar res assumint-ne un guany rellevant.

---

## 🟡 RF-11 — Reintents sense idempotència

§9 recomana 1 reintent automàtic. Si la fallada és un *timeout* de xarxa
però DeepSeek **sí** que va processar la petició, el reintent la duplica:
es paga dos cops i, pitjor, si el primer resultat arriba tard es pot
aplicar dues vegades.

**Cal** una clau d'idempotència per operació i que l'aplicació al disc
comprovi que el bloc de destí encara és el que s'esperava (el guard de
§4.3 ja ho fa — només cal assegurar que també cobreix el camí de reintent).

---

## 🟡 RF-12 — `php -l` només és sintaxi

`php -l` no detecta mètodes inexistents, aritat incorrecta ni tipus. Un
worker que inventi `$this->getClientByIdSafe()` passa la porta 1 sense
problemes i peta en producció.

Per un MVP és acceptable, però **s'ha de dir al pla**, perquè ara mateix
la porta 1 es presenta com si validés el codi i només en valida la forma.
Mitigació futura barata: comprovar que tot `$this->x(` i `Classe::x(` del
bloc nou existeixi al mapa de símbols que ja generem amb `extract.php`.
És gairebé gratis i tanca el forat més probable.

---

## 🟢 Coses que estan bé i no s'han de tocar

- **Rollback via `git hash-object`** (§4.2) — és la millor decisió del pla.
  Dedup i compressió gratis, funciona amb el working tree brut, cap format
  propi.
- **SQLite en lloc de JSONL** (§5) — correcte pels motius que dona.
- **Generació per analogia** (§4.4, solució 2) — amb 46 fitxers `Calc`
  germans, és la idea amb millor relació valor/cost de tot el document.
- **Guard de rang estancat** (§4.3) — resol un problema real, no imaginari.
- **PHP-first** (§7) — correcte, i el terreny mesurat ho confirma.

---

## Canvis que proposo al pla

| # | Canvi | Impacte |
|---|---|---|
| 1 | Eliminar portes 2 i 3; substituir-les per la **porta de substància** (RF-1, RF-2) | Alt — la seguretat actual és aparent |
| 2 | Reescriure la justificació del projecte: **volum, no context** (RF-4) | Alt — canvia què es construeix primer |
| 3 | `fix_symbols` **en lot** com a tool principal, no `fix_symbol` solt (RF-4, RF-8) | Alt |
| 4 | `patch_group` puja a Fase 0 — el lot és l'arquitectura, no una millora (RF-8) | Alt |
| 5 | Paral·lelitzar crides, serialitzar escriptures (RF-8) | Mitjà — 7 min → <1 min |
| 6 | Retorn amb diff compacte per sobre d'un llindar (RF-6) | Mitjà |
| 7 | Límit dur de mida de símbol amb rebuig explícit (RF-7) | Mitjà |
| 8 | Criteri escrit de **quan NO usar Bifrost**, dins la descripció del tool (RF-5) | Mitjà |
| 9 | Construir un smoke test de the target codebase **abans** de l'ús massiu (RF-3) | **Bloquejant** |
| 10 | El calibratge ha de mesurar `ctx` amb i sense (RF-9) | Baix, però barat |
| 11 | Clau d'idempotència als reintents (RF-11) | Baix |
| 12 | Documentar els límits de `php -l` i afegir-hi la comprovació de símbols (RF-12) | Baix |

---

## La pregunta que decideix el projecte

Res del que hi ha aquí importa si la resposta a això és dolenta:

> **Quan se li demana explícitament, DeepSeek sap tornar un bloc de codi
> real de the target codebase byte a byte intacte?**

És el cas `identitat` del calibratge. Si hi falla, l'arquitectura sencera
descansa sobre un worker que no sap deixar les coses com estan, i cap porta
de validació ho arreglarà.

**Aquesta prova s'ha d'executar abans d'escriure una sola línia del
servidor MCP.** El harness ja està escrit i només li falta la clau:

```bash
export DEEPSEEK_API_KEY=sk-...
python3 calibratge/calibra.py --casos 9
```
