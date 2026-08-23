# MCP-Bifrost — Pla de projecte (viu)

**Estat: v0.5 — contrastat amb dades reals. El calibratge s'ha executat
contra DeepSeek i n'han sortit 7 canvis, ja incorporats.**
Espec d'entrada: [`espec-original.md`](./espec-original.md) (no es toca).
📋 [`revisio-critica.md`](./revisio-critica.md) — 12 troballes amb ulls
frescos. **Incorporades.**
📊 [`../calibratge/RESULTATS.md`](../calibratge/RESULTATS.md) — calibratge
executat contra DeepSeek: premissa validada, bug de byte-offsets destapat,
RF-5 refutat i RF-10 suavitzat. **Incorporat.**
Aquest document és el que iterem fins que sigui definitiu.

---

## 1. Què és, en una frase

Un servidor MCP que rep de Claude Code feines de codi ja **analitzades i
trossejades**, n'extreu quirúrgicament el rang exacte via parser, delega
l'escriptura a DeepSeek, valida i aplica el pedaç de forma atòmica al
disc, i registra tota l'execució fora del context de Claude.

**Repartiment de rols confirmat:**
- **Tu** → parles amb Claude en llenguatge natural.
- **Claude Code** → analitza *què* s'ha de fer i *com*, decideix l'estratègia,
  trosseja la feina en unitats aïllades i les envia al MCP. També verifica el
  resultat (tests, lint, coherència).
- **MCP-Bifrost** → parseja, empaqueta, crida DeepSeek, valida, aplica, registra.
- **DeepSeek** → escriu el codi d'una unitat aïllada. No decideix res.

DeepSeek és el **múscul**; Claude és el **cap**. El MCP és el nervi que
els connecta i el que garanteix que res arriba al disc trencat.

---

## 2. Decisions tancades

| Tema | Decisió |
|---|---|
| Worker | **DeepSeek**, confirmat. Hi ha crèdits i volum de feina real. |
| Llenguatges MVP | **Python + PHP tots dos des del principi.** Java queda fora. |
| `export_docs` | **Fora d'abast ara.** Però tota execució queda registrada. |
| Format del registre | **SQLite** per l'operatiu (permanent) + **JSONL derivat** per mesurar l'eficàcia (temporal). Una sola escriptura (§5). |
| Rollback | **Lligat a git**, sempre (veure §4.2). |
| Interfície d'usuari | Claude Code. No hi ha CLI d'usuari a l'MVP. |

---

## 2.5 Reconeixement del terreny — the target codebase

Objectiu real confirmat: **el codi PHP de the target codebase**. Mesurat, no assumit:

| Mètrica | Valor |
|---|---|
| Fitxers PHP propis (sense `vendor/`, `OLD_*`, `_deploy`) | **131** |
| Línies de PHP propi | **~34.900** |
| Fitxer més gran | `src/app/AdminService.php` — 4.652 línies |
| Segon | `src/api/router.php` — 3.320 línies |
| Mètodes a `AdminService.php` | **202**, una sola classe |
| `case` al switch de `src/api/router.php` | **199** |
| Fitxers `Calc` paral·lels | **46**, mateixa forma |
| Fitxers amb HTML mesclat | **1** (`DocumentTemplate.php`) |

### Què en surt

**🟢 El risc PHP/HTML que temia a la v0.2 és marginal.** Un sol fitxer
mescla HTML. La resta és PHP pur amb classes i funcions ben formades. La
preocupació de §6 sobre haver de dependre molt de `fix_range` a PHP queda
descartada.

**🟢 `AdminService.php` és el cas d'ús perfecte.** 202 mètodes amb el patró
uniforme `public function nom(...)`, dins d'una sola classe. Adreçar per
símbol hi funcionarà gairebé sempre. I el guany és brutal: enviar un mètode
de ~30 línies en lloc d'un fitxer de 4.652 és una **reducció del ~99%**,
per sobre de l'objectiu del 85-95% de l'espec.

**🟡 El switch de `admin.php` necessita adreçament propi.** 199 `case` que
són el punt d'extensió de tota l'API, i que no són símbols PHP. Cal
tractar-los explícitament (§4.4).

**🟡 46 fitxers `Calc` germans = mina d'or per la generació.** Tenir 46
implementacions de la mateixa forma converteix "genera un `Calc` nou" en
"calca aquest" (§4.4, solució 2).

**🔴 Hi ha credencials dins l'arbre del repo.** El directori de credencials
de the target codebase conté fitxers de servei i d'usuaris que **se sincronitzen
expressament** amb el repo. El gate de detecció de secrets (§4.5) deixa de
ser una precaució teòrica: hi ha material sensible al mateix arbre pel qual
passarà l'eina. **Puja a Fase 1.**

*(Els noms concrets dels fitxers s'han retirat d'aquest document: MCP-Bifrost
està destinat a ser públic i no ha de publicar el mapa de credencials d'una
aplicació de producció.)*

**🔴 `AdminService.php` amb 4.652 línies i 202 mètodes en una classe és un
risc de concurrència.** Si Claude trosseja una feina en 15 unitats i 12
toquen aquest mateix fitxer, cal serialització estricta per fitxer (§4.6) i
el guard de rang estancat (§4.3) hi serà crític, no opcional.

---

## 3. Veredicte general

La idea és sòlida i el patró (orchestrator-worker / model cascade) és
real i provat. Amb els rols aclarits, el risc més gran de la v0.1 —que el
worker treballés a cegues— queda molt reduït: **si Claude és qui trosseja,
la qualitat del tros és responsabilitat de Claude**, i això és exactament
on ha de viure aquesta decisió.

El que queda per resoldre no és el concepte, sinó **les garanties**: que
res arribi trencat al disc, que tot sigui reversible, i que a un projecte
enorme el sistema no es converteixi en una màquina de generar regressions
silencioses a escala.

---

## 4. Solucions als forats de l'espec

### 4.0 — Regla de bytes (validada amb dades, 2026-08-23)

**Tot el camí extracció → payload → splice → escriptura treballa en `bytes`.**
Es descodifica a text només per enviar-ho al worker, i es torna a codificar
en rebre'l.

No és una preferència d'estil. `token_get_all()` de PHP retorna **offsets
en bytes**; els `str` de Python s'indexen **per caràcters**. Barrejar-los
desplaça el tall tants caràcters com bytes multibyte hi hagi abans del
símbol:

```
ShiftCalc.php:  4.506 bytes / 4.504 caràcters    ← 2 accents

slice sobre str    → 'blic function closeShift($shiftId) {'
slice sobre bytes  → 'public function closeShift($shiftId) {'
```

El calibratge va fallar **6 de 9 casos** per aquesta causa abans de
detectar-la. the target codebase té comentaris en català a tot arreu: no és un cas
límit, és el cas normal. I falla **en silenci** — retalla el principi del
bloc, n'afegeix bytes del final, i el resultat encara sembla codi.

**Qualsevol `read_text()` en aquest camí és un bug esperant un fitxer amb
accents.** Detalls a [`../calibratge/RESULTATS.md`](../calibratge/RESULTATS.md).

### 4.1 — Portes de validació abans d'escriure (resol §3.1 de la v0.1)

**Revisat a fons.** La versió anterior tenia tres portes, i
[la revisió crítica](./revisio-critica.md) (RF-1) va demostrar que dues no
podien fallar mai. El calibratge ho va confirmar amb dades: a la primera
tanda, la porta de perímetre va donar **9/9 mentre 3 fitxers quedaven
sintàcticament trencats**.

Cap byte toca el disc sense passar aquestes portes, en aquest ordre:

**Porta 0 — offsets (la crítica).**
```
fitxer[start:end] == src_que_vam_enviar     # en bytes
```
Es comprova rellegint el fitxer just abans d'escriure. Detecta que el
fitxer ha canviat des de l'extracció (un patch anterior del lot, un altre
procés, l'editor de l'usuari) i qualsevol error de càlcul de límits.

És **la porta que evita el dany més gran**: fer el splice amb offsets
equivocats injecta el bloc al mig d'un altre mètode i destrueix codi. És la
mateixa comprovació que §4.3, i el calibratge n'ha validat el valor —
hauria detectat les 9 corrupcions de la primera tanda.

**Porta 1 — sintaxi.** Es reconstrueix el fitxer sencer en memòria i es valida:
- PHP → `php -l` sobre un fitxer temporal.
- Python → `ast.parse()` (stdlib).

*Límit conegut:* només valida la forma. Un `$this->metodeInexistent()` la
passa sense problemes (RF-12). Mitigació barata per més endavant: comprovar
que tot `$this->x(` i `Classe::x(` del bloc nou existeixi al mapa que ja
genera `extract.php`.

**Porta 2 — un sol símbol.** El bloc retornat ha de contenir **exactament
una** definició de símbol. Evita que el worker afegeixi una funció germana
pel seu compte. Dues línies amb `extract.php`.

*Nota:* això és el que queda de l'antiga "porta d'integritat estructural".
La versió que comparava els símbols de **tot el fitxer** s'ha eliminat: amb
extracció per símbol el worker no veu mai el codi veí i no el pot esborrar.

**Porta 3 — substància** (abans de l'ús massiu, no al dia 1).
Comparar el conjunt de crides, variables i paraules clau de control del
bloc abans i després. Si el bloc nou n'ha perdut alguna que la instrucció
no demanava treure → rebuig, amb la llista del que ha desaparegut.

Ataca el risc real de RF-2: codi que compila però ha perdut lògica de dins
del mètode. **El calibratge no l'ha vist manifestar-se** (3/3 en tasques
additives sense perdre cap línia), i per això baixa de prioritat — però 9
casos de 5-28 línies no el descarten, i el risc creix amb la mida del bloc.

**Eliminada — porta de perímetre.** Comparar els bytes de fora del rang no
pot fallar mai: el MCP construeix el fitxer com
`original[:start] + bloc + original[end:]`, i el perímetre és idèntic **per
construcció**. Es va eliminar també el test que la verificava: un test que
no pot fallar és pitjor que cap test, perquè dona senyal positiu sense
mirar res.

Si qualsevol porta falla → `ERROR` estructurat a Claude amb el motiu, cap
escriptura, i registre de l'intent fallit amb **quina porta** l'ha rebutjat.

**Cost:** milisegons.

### 4.2 — Rollback via git object store (resol §3.2, aprofitant que ja hi ha git)

Aprofitem que git ja és una base de dades de continguts adreçada per hash.
No cal duplicar snapshots.

**Abans de cada patch:**
```
git hash-object -w <fitxer>   →  blob_sha
```
Això escriu el contingut original a `.git/objects` i torna el seu SHA.
Guardem `blob_sha` al registre. **Cost en disc: zero addicional real** —
git ja comprimeix i dedupa; si el contingut ja existia (perquè està
commitat), no s'escriu res nou.

**Per revertir:**
```
git cat-file blob <blob_sha>  →  contingut original  →  reescriure fitxer
```

**Avantatges sobre l'snapshot manual:**
- Funciona encara que el working tree estigui brut (no exigeix disciplina).
- Dedup i compressió gratis.
- Sobreviu entre sessions i és auditable amb eines git estàndard.
- Zero format propi a mantenir.

**Precaució a documentar:** aquests blobs són *unreachable* fins que es
commiten, i un `git gc --prune` agressiu els podria eliminar. Mitigació:
crear una ref (`refs/bifrost/<id>`) que els ancori, o simplement no
executar `gc --prune=now` (l'expiració per defecte és de 2 setmanes, molt
per sobre de la finestra útil d'un rollback).

**Tools de rollback:**
- `revert_patch(patch_id)` — restaura un patch concret.
- `revert_session()` — restaura tots els patches de la sessió actual, en
  ordre invers. Imprescindible quan Claude ha trossejat una feina en 20
  unitats i la 17a ha anat malament: no vols desfer-ne una, vols desfer-les
  totes.

**Integració amb GitHub:** l'MVP no fa push ni branques automàtiques. Però
el registre guarda el `HEAD` sha del moment de cada patch, cosa que permet
després agrupar-los per commit i, en una fase posterior, generar branques
o PRs per lot de canvis sense reconstruir res.

### 4.3 — Guard de rang estancat (resol §3.5)

Quan Claude envia 20 unitats sobre el mateix fitxer, les línies es mouen
sota els peus.

**Solució:** cada patch registra el `sha256` del bloc que espera trobar al
rang indicat. Just abans d'escriure, el MCP rellegeix el fitxer i el
recalcula. Si no coincideix → `ERROR: stale range` amb el contingut actual
del rang, i Claude re-resol.

**Millora forta:** afegir `fix_symbol` **des del MVP**, no com a fase 2.
Adreçar per símbol (`nom_de_funcio`) en lloc de per línia és immune al
desplaçament de línies. Amb Claude trossejant feina en lots grans sobre el
mateix fitxer, `fix_symbol` no és un luxe — és el mode d'ús principal.
`fix_range` queda com a escapatòria per a codi que no viu dins d'un símbol
(configuració a nivell de mòdul, blocs HTML dins de PHP, etc.).

### 4.4 — Generació de codi nou (el forat de l'espec original)

L'espec només contempla **substituir** codi existent. Tu has dit "corregir
**i generar**". Amb el terreny reconegut (§2.5), aquest forat té una
solució molt més concreta del que semblava.

#### L'observació que ho canvia tot

A the target codebase, **generar codi nou gairebé mai és escriure en blanc**. És
replicar un patró que ja existeix 46 vegades, en un punt d'ancoratge
estructural conegut. Afegir una entitat nova al sistema vol dir, quasi
sempre, la mateixa seqüència:

```
1. src/calc/NouCalc.php        ← fitxer nou, germà de 46 iguals
2. src/app/AdminService.php        ← require_once + 4-6 mètodes CRUD
3. src/api/router.php            ← 4-6 case nous al switch + funcions
4. public/js/…                     ← frontend
```

Això no és "generació lliure". És **inserció ancorada** i **replicació per
analogia**. Dos problemes tractables, no un de difús.

#### Solució 1 — Inserció ancorada

`insert_symbol(file_path, anchor_symbol, position, instruction)`

- `anchor_symbol`: un símbol existent (`updateClient`).
- `position`: `before` | `after` | `end_of_class` | `end_of_file`.

Ancorar a un símbol i no a un número de línia és el que fa que això
funcioni en un fitxer de 4.652 línies que Claude està modificant en lot:
l'àncora no es mou encara que tot el que hi ha a sobre canviï.

`insert_case(file_path, switch_anchor, after_case, instruction)`

Cas específic però imprescindible: `src/api/router.php` té un `switch`
amb **199 `case`**. És el patró d'extensió principal de tota l'API i no és
un símbol PHP — cap parser te'l donarà com a tal. Es resol com a bloc
delimitat entre dos `case` germans, que tree-sitter sí sap identificar.

Sense això, cada endpoint nou obliga a `fix_range` sobre un fitxer de
3.320 línies, que és exactament el cas fràgil que volem evitar.

#### Solució 2 — Generació per analogia (`create_file`)

`create_file(file_path, model_from, instruction)`

En lloc de descriure des de zero què ha de contenir `ExtraWarrantyCalc.php`,
li passes `WarrantyCalc.php` com a **exemplar estructural** i una
instrucció de diferència. DeepSeek no inventa arquitectura: la calca.

Per què és la peça forta d'aquest disseny:
- **Cost en tokens baix**: un fitxer Calc ronda les 100-200 línies. Un sol
  exemplar és tot el context que cal.
- **Consistència altíssima**: el codi nou surt idèntic en estil,
  convencions de noms, gestió d'errors i estructura als 46 germans. És
  precisament on un worker barat amb poc context sol fallar, i aquí no pot.
- **Verificable**: es pot comparar el conjunt de mètodes del fitxer generat
  amb el de l'exemplar i avisar de desviacions.

Aquesta tècnica converteix la debilitat del worker (no sap res del
projecte) en irrellevant: no li cal saber-ho, li cal copiar bé.

#### Solució 3 — Portes de validació adaptades a inserció

La porta de perímetre de §4.1 assumeix substitució: comparar tot el que hi
ha fora del rang. En una inserció els desplaçaments canvien i la
comprovació s'ha de partir en dues:

- **Perímetre partit**: tot el que hi ha *abans* del punt d'inserció ha de
  ser idèntic byte a byte, i tot el que hi ha *després* també. L'única
  diferència admesa és contingut afegit al punt exacte. Segueix sent una
  garantia dura, i segueix sent barata.
- **Porta estructural**: el conjunt de símbols preexistents ha de
  mantenir-se **intacte**, més exactament els N nous declarats. Si la
  inserció s'ha "menjat" el mètode veí — el error més típic quan un model
  reescriu una regió— es detecta aquí.
- **Porta sintàctica**: igual (`php -l`).

Per `create_file`: no hi ha perímetre, però sí sintàctica, i s'hi afegeix
una porta pròpia: **el fitxer no ha d'existir**. Mai sobreescriure via
`create_file`.

#### Solució 4 — Transaccions multi-fitxer (el forat dins del forat)

Aquest és el problema real que la llista de dalt destapa: **generar una
funcionalitat nova a the target codebase toca 3-4 fitxers alhora i no té sentit fer-ho
a mitges.** Un `NouCalc.php` creat però sense el `require_once` a
`AdminService.php` deixa el projecte pitjor que abans de començar.

Proposta: `patch_group`.

- Claude obre un grup, hi envia N operacions (patches, insercions,
  creacions), i el tanca.
- El MCP les valida **totes** abans d'aplicar-ne cap.
- O s'apliquen totes, o cap. Si la 3a de 4 falla una porta, les dues
  primeres es reverteixen automàticament.
- El rollback ja el tenim gratis: els blobs de git de §4.2, revertits en
  ordre invers. Els fitxers creats s'esborren.

Sense això, l'eina és fiable a nivell de fitxer i fràgil a nivell de
funcionalitat — que és el nivell on tu realment treballes.

#### Ordre recomanat

`insert_symbol` i `create_file` són els que donen valor immediat sobre
the target codebase i són els més senzills. `insert_case` va lligat a l'API i és
específic però d'alt rendiment. `patch_group` és el que fa que tot plegat
sigui segur en feines reals, i hauria d'arribar abans que es faci servir
intensivament, no després.

### 4.4b — Normalització d'indentació al payload (validat amb dades)

**El worker no ha d'endevinar mai la indentació.** El MCP li treu abans
d'enviar i la torna a posar en rebre:

```
extret     "    public function foo() {\n        return 1;\n    }"
   ↓  normalitza (indent="    ")
enviat     "public function foo() {\n    return 1;\n}"
   ↓  worker
rebut      "public function foo() {\n    return 2;\n}"
   ↓  re-indenta amb indent
aplicat    "    public function foo() {\n        return 2;\n    }"
```

El payload porta un camp `indent` **obligatori des del dia 1**, encara que
una implementació concreta no l'usi.

**Per què no és una precaució teòrica:** al calibratge, 1 de 9 casos va
tornar el bloc amb indentació diferent de l'original. Tota la resta era
correcte —compilava, signatura intacta, cap línia perduda— però el format
s'havia mogut. En 35.000 línies, el cosmètic acumulat és deute.

I és **el mateix problema que §11 preveia per a Python**, aparegut abans en
PHP. Allà és pitjor: la indentació de Python és semàntica, i un bloc mal
re-indentat no és lleig, és codi trencat. Resoldre-ho ara al payload tanca
els dos casos alhora.

### 4.5 — Detecció de secrets abans d'enviar (resol §3.4)

Amb DeepSeek confirmat i un projecte enorme de producció, això passa de
"suggeriment" a **obligatori**. Escaneig del bloc `src` i del `ctx` abans
de la crida, cercant patrons de: claus d'API, tokens bearer, cadenes de
connexió amb credencials, claus privades, contrasenyes en literals.

Si es detecta → `ERROR: secret detected`, no s'envia, i s'avisa Claude. Fals
positiu? Es pot forçar amb un flag explícit per crida. La clau és que el
comportament per defecte sigui **no filtrar**, i que saltar-se'l sigui una
decisió conscient i registrada.

### 4.6 — Límit de cost i concurrència (resol §4.5 de la v0.1)

Amb Claude trossejant feina automàticament sobre un projecte enorme, el
volum de crides pot disparar-se sense que ningú se n'adoni.

- Comptador de crides i tokens per sessió, amb tall dur configurable.
- **Paral·lelisme controlat:** si Claude envia un lot de N unitats
  independents, el MCP les pot processar en paral·lel (semàfor, ex. 4-6
  simultànies).

  **Serialitzar l'escriptura, no la crida** (RF-8). Les crides al worker
  són independents —cada símbol es processa aïllat— i només l'aplicació al
  disc necessita ordre. Serialitzar per fitxer, com deia la versió
  anterior, no s'activava mai on cal: el target principal és
  `AdminService.php`, un sol fitxer amb 200 símbols, i un lot de 30 correccions
  hi hauria anat sencer en sèrie.

  **Latència mesurada: 2,6s de mitjana per crida** (calibratge, 2026-08-23).
  30 crides en sèrie ≈ 78s; en paral·lel de 6, ≈ 13s. La latència per crida
  no és un problema —és comparable a un `Edit` de Claude— però multiplicada
  per un lot sí que ho és.

---

## 5. Registre — dos propòsits, dues vides, **una sola escriptura**

La divisió és correcta, perquè són dues coses amb **vides diferents**:

| | Registre operatiu | Registre d'eficàcia |
|---|---|---|
| Per a què | saber què s'ha fet, i poder desfer-ho | provar si el projecte val la pena |
| Vida | permanent, creix per sempre | **temporal — s'esborra quan la premissa quedi provada o refutada** |
| Com es llegeix | consultes puntuals (per fitxer, per data) | sencer, d'un cop |
| Format | **SQLite** `.bifrost/history.db` | **JSONL** `.bifrost/eficacia.jsonl` |

Que el segon tingui data de caducitat és precisament el que justifica que
sigui a part. Un instrument de mesura no ha de viure dins del sistema que
mesura.

### El matís important: el JSONL es **deriva**, no s'escriu en paral·lel

Escriure els dos fitxers a cada operació sembla innocu i no ho és. Si el
procés cau entre les dues escriptures —o una falla i l'altra no— els dos
registres divergeixen, i **no hi ha manera de saber quin dels dos té raó**.
És el mode de fallada clàssic de tenir dues fonts de veritat per la mateixa
dada, i apareix just quan més falta fa el registre: quan alguna cosa ha
anat malament.

Regla: **SQLite és l'única escriptura.** El JSONL d'eficàcia és una
**exportació** que es genera a demanda a partir de SQLite. Cost: una
consulta. Guany: divergència impossible per construcció.

```
operació → SQLite (única escriptura, transaccional)
                │
                └── export ──> eficacia.jsonl ──> registre.py
```

Això no treu res del que volies: segueixes tenint el JSONL lleuger,
llegible i separat per mesurar l'estalvi. Només canvia **d'on surt**.

### Esquema operatiu (SQLite)

```sql
CREATE TABLE patches (
  id           TEXT PRIMARY KEY,   -- uuid4
  ts           TEXT NOT NULL,      -- ISO-8601 UTC
  session      TEXT,               -- agrupa un lot
  grup         TEXT,               -- patch_group, si n'hi ha (§4.4)
  op           TEXT NOT NULL,      -- fix_symbol | fix_range | create_file…
  fitxer       TEXT NOT NULL,
  simbol       TEXT,
  start_byte   INTEGER,
  end_byte     INTEGER,
  estat        TEXT NOT NULL,      -- ok | rebutjat | error
  porta        TEXT,               -- quina porta l'ha rebutjat, si escau
  blob_abans   TEXT,               -- git hash-object → rollback (§4.2)
  head_sha     TEXT,               -- HEAD del repo en aquell moment
  instruccio   TEXT,               -- què se li va demanar
  rationale    TEXT,               -- el `why` del worker
  src_b        INTEGER,            -- ── mesures per a l'eficàcia ──
  out_b        INTEGER,
  in_b         INTEGER,
  resp_b       INTEGER,
  tin          INTEGER,
  tout         INTEGER,
  cache_hit    INTEGER,            -- tokens servits de cache (43% mesurat)
  ms           INTEGER
);
CREATE INDEX idx_fitxer  ON patches(fitxer);
CREATE INDEX idx_ts      ON patches(ts);
CREATE INDEX idx_session ON patches(session);
CREATE INDEX idx_estat   ON patches(estat);
```

Es registra **tot**, també els intents rebutjats i per quina porta (§9,
pregunta 3). És on hi ha la informació per calibrar el sistema.

`PRAGMA journal_mode=WAL` — escriptura concurrent segura quan el lot va en
paral·lel (RF-8).

### Registre d'eficàcia (JSONL derivat)

Només les columnes de mesura. Cap text lliure: ni `instruccio` ni
`rationale`, que és el que faria créixer el fitxer i no cal per comptar.

```json
{"ts":"2026-08-23T14:02:11Z","op":"fix_symbol","f":"src/app/AdminService.php",
 "sym":"AdminService::updateRecord","ok":true,"src_b":812,"out_b":905,
 "in_b":142,"resp_b":58,"tin":298,"tout":241,"ms":6210}
```

~150 bytes per feina. 10.000 feines ≈ 1,5 MB.

### Principi que es manté: mesures, no conclusions

Enlloc es desa `estalvi: 1850`. Es desen els bytes, i l'estalvi es deriva
en llegir. Quan la fórmula resulti optimista —i ho serà, RF-4— es corregeix
sense invalidar res del que ja s'ha registrat.

```
cost_si_ho_fes_claude  ≈ (src_b + out_b) / 3.5
cost_real_per_claude   ≈ (in_b + resp_b) / 3.5
estalvi                = cost_si_ho_fes_claude - cost_real_per_claude
```

**Cota superior**, i etiquetada com a tal: si Claude ja havia llegit el
bloc per formular la instrucció, `src_b` ja estava pagat.

### Quan s'esborra el JSONL

Quan es respongui la pregunta que existeix per respondre: *l'estalvi
acumulat justifica la latència i el risc?* Si la resposta és que sí, el
número ja no cal seguir-lo mirant. Si és que no, el projecte s'ha de
replantejar i tampoc cal. En tots dos casos, fora.

---

## 6. Parsers — decisió revisada amb dades

**La v0.4 deia tree-sitter per a l'extracció. Es descarta per a PHP.**

Motiu: hi ha una opció millor que ja teníem al davant. `token_get_all()` és
el tokenitzador **oficial** de PHP —el mateix que fa servir l'intèrpret— i
el binari `php` ja és una dependència obligatòria per la validació (`php -l`).

| | tree-sitter-php | `token_get_all()` |
|---|---|---|
| Dependències | paquet Python + gramàtica | **cap** (el binari `php` ja hi és) |
| Fidelitat | gramàtica de tercers | **el lèxer oficial** |
| Instal·lació en aquesta màquina | ❌ sense `pip` | ✅ funciona |
| Provat sobre the target codebase | — | **128/128 fitxers, 0 fallades** |

`calibratge/extract.php` ja ho implementa: retorna nom, classe, FQN,
offsets de byte, línies, indentació i l'inici del docblock previ. Inclou una
comprovació de sanitat que la concatenació dels tokens reconstrueix el
fitxer byte a byte — si no quadra, avorta en lloc de tornar offsets dubtosos.

**Validació:** natiu de cada llenguatge. `php -l` per PHP, `ast.parse()` per
Python. Aquí la fidelitat importa més que la uniformitat: el parser oficial
és l'única autoritat sobre si un fitxer és vàlid.

**Per a Python** (§11), quan arribi: `ast` per les dues coses. També stdlib,
també oficial, i amb `lineno`/`end_lineno` exactes. La simetria es manté —
**cada llenguatge s'analitza amb les seves pròpies eines oficials** — que
resulta ser més coherent que forçar una gramàtica comuna de tercers.

**El que això no canvia:** l'abstracció `LanguageAdapter` segueix sent
necessària (§11). El que varia entre llenguatges ara és la *implementació*
de l'adaptador, no la seva forma.

**Nota sobre PHP mesclat amb HTML:** mesurat, només 1 de 131 fitxers ho fa
(§2.5). `token_get_all()` ho tokenitza correctament de totes maneres
(`T_INLINE_HTML`), així que ni tan sols cal tractar-ho com a cas especial.

---

## 7. Blueprint revisat — PHP primer

Reordenat amb l'objectiu real confirmat. **Python deixa de ser el punt de
partida**: era la ruta més barata d'implementar, però no és on hi ha la
feina. Es manté al disseny (l'abstracció de parsers el contempla) però
s'implementa després.

**Fase 0 — El bucle, complet i segur, sobre PHP**
- Tools: `fix_symbol`, `fix_range`.
- **Tot el camí de dades en `bytes`** (§4.0). No negociable.
- Extracció amb **`extract.php` / `token_get_all()`**, no tree-sitter
  (§6 — decisió revisada amb dades). Validació amb `php -l`.
- Worker DeepSeek amb el schema sintètic de l'espec, **més el camp
  `indent`** (§4.4b).
- Portes 0, 1 i 2 (§4.1). La porta 3 (substància) queda per Fase 1.
- Registre SQLite + blob previ a git (§4.2, §5).
- **Banc de proves real**: `src/calc/*.php` (fitxers petits i uniformes,
  baix risc) abans de tocar `AdminService.php`. Ja usat al calibratge.
- **Criteri de sortida:** 10 correccions encadenades sobre `AdminService.php`
  sense una sola corrupció, amb `offsets_ok` verificat a cada pas.

**Fase 1 — Blindatge (puja des de Fase 4)**
- Detecció de secrets abans d'enviar (§4.5). **No opcional**: hi ha
  credencials sincronitzades dins l'arbre del repo (§2.5).
- **Porta de substància** (§4.1, porta 3). No es va manifestar al
  calibratge, però ha d'existir abans de l'ús massiu i abans de blocs grans.
- Serialització de l'**escriptura** (no de la crida) i límits de cost (§4.6).
- `revert_session()`.
- **Smoke test de the target codebase** (RF-3) — **bloquejant** per a qualsevol ús
  massiu sobre `Calc/`, `App/` o `api/`. És l'única xarxa semàntica que hi
  haurà, i ara mateix no existeix.

**Fase 2 — Generació sobre PHP**
- `insert_symbol` i `create_file` amb generació per analogia (§4.4).
- `patch_group` — transaccions multi-fitxer. Ha d'arribar **abans** de
  l'ús intensiu, no després.
- Primera prova real: generar un `Calc` nou complet amb el seu cablejat.

**Fase 3 — L'API**
- `insert_case` per al switch de 199 casos de `src/api/router.php`.

**Fase 4 — Escala i Python**
- Paral·lelisme per lots (§4.6).
- Suport Python (`ast` per validar, tree-sitter per extreure).

**Fora d'abast (per ara)**
- `export_docs` i generació de CHANGELOG.
- Java.
- Preview/confirmació de diffs grans (Claude ja és al bucle i pot demanar
  el diff si el vol).
- Automatització de branques/PRs a GitHub.

---

## 8. Assumpcions — veredicte actualitzat

| Assumpció | Veredicte |
|---|---|
| Reduir tokens del context de Claude aporta valor real | ✅ Confirmat, i més amb un projecte enorme: el coll d'ampolla és el context, no la intel·ligència. |
| Claude sap trossejar bé la feina | ✅ Assumible — és exactament la tasca on és fort. Però cal que el MCP li retorni errors **accionables**, no `ERROR` opac. |
| `ctx` amb signatures n'hi ha prou pel worker | ⚠️ Depèn del tros. Amb Claude decidint el `ctx`, el control hi és; cal que el schema li permeti enviar tant signatures com blocs sencers quan calgui. |
| DeepSeek com a worker | ✅ **Validat amb dades**: 9/9 JSON vàlid, 3/3 identitat byte a byte, 0/9 tanques markdown, 2,6s de mitjana. Risc residual = privacitat (§4.5). |
| Un sol schema serveix per Python i PHP | ✅ Millor del previst: només 1 de 131 fitxers PHP mescla HTML (§2.5). El punt fràgil real no és PHP sinó la indentació — i el calibratge l'ha vist fallar **ja en PHP** (§4.4b). |
| Preservació del perímetre al 100% | ⚠️ **Replantejat.** És gratis per construcció i verificar-ho no prova res (RF-1, confirmat al calibratge). El que cal verificar són els **offsets** (§4.1, porta 0). |

---

## 9. Preguntes — resoltes i pendents

**1. Primer objectiu real** — ✅ **Resolt.** El codi PHP de the target codebase.
Conseqüència: PHP passa a Fase 0, detecció de secrets puja a Fase 1,
Python baixa a Fase 4 (§11).

**2. Tolerància a errors del worker** — pendent, amb recomanació.
Recomanació: **1 reintent automàtic**, i només quan l'error és de porta
sintàctica o estructural (són errors que el worker pot corregir si li
dius exactament què ha trencat). Errors de perímetre → cap reintent,
directe a Claude: significa que el worker ha sortit del seu carril i
insistir-hi és pitjor. Amb crèdits de DeepSeek disponibles, el cost d'un
reintent és negligible comparat amb el cost de context de fer tornar
Claude al bucle.

**3. Granularitat del registre** — pendent, amb recomanació.
Recomanació: **registrar-ho tot**, inclosos intents fallits i quina porta
els ha rebutjat. És l'única manera de saber, després de 200 patches, si
el sistema falla per prompts pobres, per context insuficient o per límits
del worker. Cost en disc: irrellevant amb SQLite.

**4. Pressupost de proves** — ✅ **Resolt.** Hi ha crèdits de DeepSeek
disponibles per aquest projecte, i es poden gastar en proves no
estructurades. Conseqüència directa sobre el disseny:

- **Fase 0 es valida contra l'API real, no contra mocks.** El risc més
  gran d'aquest projecte no és el codi del MCP, és *què torna realment el
  worker* davant d'un mètode de `AdminService.php`. Això no es descobreix amb
  un mock.
- Val la pena una **prova de calibratge abans d'escriure el servidor**:
  agafar 5-10 mètodes reals de `Calc/*.php`, enviar-los a mà amb el schema
  sintètic proposat, i mesurar taxa d'encert, respecte de l'estil, i si
  el JSON de sortida és fiable. Una tarda de feina que pot invalidar o
  confirmar tota la premissa del projecte per un cost de cèntims.
- També es pot usar DeepSeek com a worker **durant la construcció del
  propi MCP-Bifrost**, no només després.

---

## 10. Historial d'iteracions

- **2026-08-23** — v0.1: revisió inicial de l'espec. Alertes i blueprint MVP.
- **2026-08-23** — v0.2: decisions tancades (DeepSeek confirmat, Python+PHP,
  `export_docs` fora d'abast, registre SQLite, rollback via git object
  store). Solucions concretes als forats. Detectat forat nou: generació de
  codi no contemplada a l'espec original. `fix_symbol` puja al MVP.
- **2026-08-23** — v0.3: objectiu real = PHP de the target codebase. Terreny mesurat
  (§2.5). Solucions al forat de generació: inserció ancorada, generació per
  analogia, portes adaptades i `patch_group` (§4.4).
- **2026-08-23** — v0.4: blueprint reordenat a PHP-first (§7). Preguntes 1 i
  4 resoltes; crèdits de DeepSeek disponibles → Fase 0 es valida contra
  l'API real i s'afegeix una prova de calibratge prèvia. Analitzat el cost
  d'afegir Python (§11).

---

## 11. Què suposaria afegir Python a la barreja

Resposta curta: **poc, si el disseny es fa bé des del principi; molt, si
s'improvisa després.** El cost no és el parser de Python — és no haver
deixat el forat preparat.

### Què costa de veritat

| Peça | Cost d'afegir Python | Comentari |
|---|---|---|
| Extracció de símbols | **Baix** | `tree-sitter-python` és una gramàtica més. Si l'extractor ja parla tree-sitter per PHP, és canviar de gramàtica i un mapa de tipus de node. |
| Validació sintàctica | **Molt baix** | `ast.parse()` és stdlib. Més barat i més fiable que `php -l`, que necessita subprocés. |
| Portes de perímetre i estructural | **Zero** | Operen sobre bytes i sobre llistes de símbols. Són agnòstiques del llenguatge per construcció. |
| Registre, rollback git, `patch_group` | **Zero** | No saben ni quin llenguatge estan tocant. |
| Prompt del worker | **Baix-mitjà** | Cal una variant per llenguatge (convencions, què és una signatura, com es declara context). Poc text, però sí calibratge propi. |
| Detecció de secrets | **Zero** | Patrons sobre text. |

### On sí que hi ha feina real

**1. La indentació de Python és semàntica.** És l'única diferència
conceptual seriosa. A PHP pots inserir un mètode amb la clau al lloc i
funciona; a Python, si el bloc retornat pel worker ve amb 0 espais
d'indentació i l'has d'encaixar dins d'una classe, cal **re-indentar-lo**
abans d'inserir-lo. I si el worker barreja tabs i espais, trenques el
fitxer.

Mitigació: normalitzar la indentació del bloc extret a 0 abans d'enviar-lo,
i re-aplicar el nivell original en rebre'l. Així el worker sempre veu codi
a nivell superior i mai ha d'endevinar el context d'indentació.

✅ **Ja resolt** — vegeu §4.4b. El calibratge va veure el worker fallar la
indentació **ja en PHP** (1 de 9 casos), així que la normalització entra a
Fase 0 i no com a preparatiu per a Python. Quan Python arribi, aquesta peça
ja hi serà, que és exactament el que aquesta secció demanava.

**2. `insert_symbol` amb `position: after`** ha de saber on acaba de veritat
un símbol Python. A PHP la clau de tancament ho marca sense ambigüitat; a
Python el final d'una funció és "la primera línia amb indentació menor o
igual", i els comentaris i línies en blanc del final són territori
disputat. Tree-sitter ho resol, però cal decidir explícitament si els
comentaris posteriors pertanyen al símbol o al següent.

**3. `create_file` per analogia funciona igual de bé**, però Python té
convencions que PHP no (docstrings, `__init__`, type hints). El prompt
per llenguatge ho ha de recollir.

### Cost estimat

Si Fase 0-3 es construeixen amb l'abstracció de llenguatge com a
interfície real (`LanguageAdapter` amb `extract_symbol`, `validate`,
`list_symbols`, `normalize_indent`), afegir Python és **un adaptador nou i
una variant de prompt**. Petit.

Si en canvi es codifica PHP directament dins de la lògica dels tools
("total, ja ho generalitzarem"), afegir Python vol dir refactoritzar el
nucli sencer, amb el sistema ja en ús sobre codi de producció. Car i
arriscat.

### Recomanació

**Definir `LanguageAdapter` a Fase 0, amb una sola implementació (PHP).**
No implementis Python encara, però no facis cap decisió que l'impedeixi:
mantén el camp `indent` al payload des del primer dia (§4.4b — PHP sí que
l'usa, resulta), treballa sempre en `bytes` (§4.0 — a Python `ast` dona
offsets de caràcters, i barrejar-los serà el mateix bug al revés), i no
deixis que cap tool assumeixi claus de tancament.

Amb això, quan Python faci falta, és un dia de feina. Sense això, és una
reescriptura.
- **2026-08-23** — v0.5: calibratge executat contra DeepSeek. Premissa
  validada (3/3 identitat byte a byte). Destapat un bug de **byte offsets vs
  índexs de caràcters** que feia fallar 6 de 9 casos → nova §4.0. Portes de
  validació reescrites (§4.1): eliminada la de perímetre, afegida la
  d'offsets com a crítica. Nova §4.4b (normalització d'indentació).
  Extracció canviada de tree-sitter a `token_get_all()` (§6). RF-5 refutat
  (latència real 2,6s), RF-10 suavitzat (43% de cache).
