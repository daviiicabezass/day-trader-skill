---
name: trader-gold
description: >-
  Day trading de oro (XAUUSD) para MetaTrader 5, en español. Calcula la
  ruptura del rango de apertura de la sesión de Londres (08:00 hora de
  Londres) o de Nueva York (08:20, apertura de COMEX), en largo o en corto,
  con velas de 5 minutos del CSV exportado de tu MT5 (los precios de tu
  bróker) o de Yahoo GC=F (futuros, con ajuste de base). Fija el stop y el
  objetivo, reproduce el resto de la sesión 10.000 veces con las sesiones
  pasadas más el historial real de la misma regla, calcula los lotes según
  tu pérdida máxima, el spread, el tamaño del contrato y el margen, y escribe
  el ticket para colocarlo A MANO en MT5. Nunca envía una orden. Úsala cuando
  el usuario diga "oro", "XAUUSD", "gold", "rango de apertura de Londres",
  "apertura de Nueva York en el oro", "cuántos lotes", "orden para MT5",
  "MetaTrader", o quiera usar el flujo del pack con el oro.
argument-hint: "[clock | orb | plan | replay | size | ticket] [--session london|ny]"
---

# trader-gold

El resto del pack está hecho para acciones de EE. UU.: horario de la NYSE,
acciones enteras y Alpaca. El oro es distinto: cotiza casi 24 horas, se opera
en lotes (normalmente 1 lote = 100 onzas), en largo o en corto, y la mayoría lo
opera como CFD en MetaTrader 5. Esta skill hace el mismo recorrido (setup,
entrada, salida, Máquina del Tiempo, tamaño, ticket) para XAUUSD, en una sola
herramienta. Nada aquí coloca una orden: el ticket lo colocas tú en MT5, si
quieres. La mayoría de la gente que hace day trading pierde dinero, y el oro
con apalancamiento puede moverse muy rápido.

`${CLAUDE_SKILL_DIR}` abajo es la carpeta de esta skill (donde vive este SKILL.md).

```bash
python3 "${CLAUDE_SKILL_DIR}/gold.py" clock                                  # mercado del oro y las dos sesiones
python3 "${CLAUDE_SKILL_DIR}/gold.py" orb --session london --csv XAUUSD_M5.csv --write
python3 "${CLAUDE_SKILL_DIR}/gold.py" orb --session ny --basis -25 --write   # Yahoo GC=F ajustado a spot
python3 "${CLAUDE_SKILL_DIR}/gold.py" plan --stop or-mid --target 2R --write  # -> trader/gold-plan.json
python3 "${CLAUDE_SKILL_DIR}/gold.py" replay --csv XAUUSD_M5.csv --write      # 10.000 repeticiones + historial
python3 "${CLAUDE_SKILL_DIR}/gold.py" size --write                            # lotes -> trader/gold-order.json
python3 "${CLAUDE_SKILL_DIR}/gold.py" ticket                                  # la orden para MT5, en claro
python3 "${CLAUDE_SKILL_DIR}/gold.py" orb --session tarde --write             # la tarde: 15:30-20:00 hora de Espana
python3 "${CLAUDE_SKILL_DIR}/gold.py" journal add                             # apunta la senal en el diario
python3 "${CLAUDE_SKILL_DIR}/gold.py" journal check                           # cierra las abiertas con las velas
python3 "${CLAUDE_SKILL_DIR}/gold.py" journal show                            # el diario y los totales
```

## Las tres sesiones

| sesión | rango de apertura | entradas hasta | fuera a las | objetivo por defecto |
| --- | --- | --- | --- | --- |
| `london` | 08:00-08:15 Londres | 11:00 Londres | 16:00 Londres | 2R |
| `ny` | 08:20-08:35 Nueva York | 11:30 Nueva York | 13:25 Nueva York | 2R |
| `tarde` | 09:30-09:45 Nueva York (15:30-15:45 en España) | 12:30 NY (18:30 España) | 13:55 NY (19:55 España) | 1R |

`tarde` sigue el horario de Nueva York: las pocas semanas al año en que EE. UU.
y Europa cambian de hora en fechas distintas (marzo y finales de octubre) en
España empieza a las 14:30. En sus primeras pruebas el objetivo 1R tuvo mejor
historial que el 2R en esta franja; `--target` lo cambia.

## El diario (para probar en demo)

`journal add` apunta la señal del plan actual (con los lotes de `size` si
coinciden). `journal check` repasa las abiertas con las velas: objetivo,
stop (si una vela toca los dos, cuenta el stop) o cierre al precio de la
hora `flat_by`, con su R y sus dólares (con spread, sin swap ni comisiones).
`journal show` da la tabla y los totales. Apunta TODAS las señales, ganen o
pierdan: con menos de 30 operaciones el resultado dice poco. Se guarda en
`trader/gold-journal.json`.

Opciones comunes: `--now "2026-10-19 08:30 london"` (o `ny`, `utc`; sin zona
es Nueva York) para fijar el reloj, `--json`, `--config RUTA`. `orb` sale con
0 si se activó y 1 si no; cualquier error es una línea "No se pudo: ..." y
salida 2.

## Los datos

| fuente | cómo | ojo |
| --- | --- | --- |
| CSV de MT5 (recomendado) | En MT5: Ver > Símbolos > Barras, elige XAUUSD, M5 (o M1), Exportar. `--csv ARCHIVO` | La hora del CSV es la del servidor del bróker. `--server-tz ny+7` (por defecto) es la convención GMT+2/GMT+3 que cierra la semana a las 17:00 de Nueva York. Si tu bróker usa otra, pon `+2`, `0`, etc. |
| Yahoo `GC=F` | por defecto si no hay `--csv`; `--offline ARCHIVO` lee uno guardado | Son futuros de COMEX: cotizan unos dólares por encima del spot. `--basis -25` resta 25 a todo. Las distancias (riesgo, objetivo) valen; los niveles exactos, mejor del CSV. Gratuito, no oficial, puede ir con retraso. |

Las velas de 1 minuto se agrupan en velas de 5 minutos. El tick en vivo nunca
cuenta como vela, y una vela solo cuenta cuando ha cerrado.

## La configuración: `trader/gold.json`

Todo es opcional; estos son los valores por defecto:

```json
{
  "symbol": "XAUUSD", "contract_size": 100, "lot_min": 0.01, "lot_step": 0.01, "lot_max": 5.0,
  "balance": 5000, "max_loss_usd": 50, "leverage": 100, "spread": 0.30, "cash_buffer_pct": 10,
  "min_reward_risk": 1.0, "or_minutes": 15, "server_tz": "ny+7", "yahoo_symbol": "GC=F", "basis": 0,
  "sessions": {"london": {"open": "08:00", "no_entries_after": "11:00", "flat_by": "16:00"},
               "ny": {"open": "08:20", "no_entries_after": "11:30", "flat_by": "13:25"}}
}
```

Pregúntale al usuario, en lenguaje claro: el nombre del símbolo en su MT5
(XAUUSD, GOLD, XAUUSD.r...), el tamaño del contrato (en MT5: clic derecho en el
símbolo > Especificación; casi siempre 100 onzas), el saldo, la pérdida máxima
por operación, el apalancamiento y el spread típico. Las horas de cada sesión
van en la hora local de esa sesión (Londres o Nueva York); el horario de
verano de cada ciudad se calcula en la herramienta.

## Las reglas

- **Rango de apertura.** Máximo y mínimo de los primeros 15 minutos de la
  sesión (`--or-minutes`). Londres: 08:00 a 08:15 hora de Londres. Nueva York:
  08:20 a 08:35 hora de Nueva York.
- **La activación** es la primera vela de 5 minutos cerrada después del rango
  que CIERRA por encima del máximo (largo) o por debajo del mínimo (corto).
  `--side long` o `--side short` limita a un lado. La hora es el cierre de esa
  vela. Una mecha no cuenta.
- **Entradas** solo hasta `no_entries_after`; después, el setup caduca.
- **El stop**: `or-mid` (punto medio del rango, por defecto), `or-opposite`
  (el otro extremo), `price:X` o `usd:X` (X dólares desde la entrada). Se
  redondea alejándose de la entrada. Se rechaza un stop de menos de 3 veces el
  spread: el ruido normal te sacaría.
- **El objetivo**: `2R` por defecto, o `1R`, `1.5R`, `price:X`.
- **Fuera** a la hora `flat_by`, se gane o se pierda.
- **Los lotes** son el menor de: pérdida máxima / ((riesgo por onza + spread) x
  onzas por lote), el margen disponible tras el colchón de efectivo, y
  `lot_max`, redondeado hacia abajo al paso de lote. Si ni el lote mínimo
  cabe, no hay orden.

## Pasos

1. `gold.py clock`: ¿está abierto el oro y en qué punto está cada sesión?
2. Antes de la sesión, mira el calendario económico: NFP, IPC, la Fed y los
   datos de EE. UU. de las 8:30 de Nueva York mueven el oro de golpe. Si hay
   uno dentro de la operación, dilo.
3. `gold.py orb --session london --write` (o `ny`). Dile al usuario el estado
   en una línea. Para esperar, `/loop 5m` con el mismo comando.
4. Cuando se active: `gold.py plan --write`. Lee en voz alta la entrada, el
   stop y su regla, el objetivo, el riesgo por onza y la hora de salida.
5. `gold.py replay --write`. Repite las probabilidades, el historial y el
   veredicto tal como salen, y la semilla. Si es lanzar una moneda, dilo.
6. `gold.py size --write`: los lotes, qué límite los decidió, la pérdida
   máxima con spread y el margen.
7. `gold.py ticket`: muéstralo. El usuario decide si la coloca en MT5. Nunca
   lo presiones.

## Ejemplo

Una sesión de Londres con rango de 2648,00 a 2655,00: la vela de las 08:20
cierra en 2655,20, por encima del máximo. Activado largo a las 08:25 (Londres)
en 2655,20. Stop en el punto medio, 2651,50 (3,70 por onza); objetivo 2R,
2662,60. Con 5.000 $, 50 $ de pérdida máxima y 0,30 de spread:
(3,70 + 0,30) x 100 = 400 $ por lote, 50 / 400 = 0,125, así que 0,12 lotes;
pérdida máxima en el stop unos 48 $.

## Lo que nunca hace

- Nunca coloca, modifica ni cierra una orden en MT5 ni en ningún bróker.
- Nunca dice que una operación va a ganar ni da una predicción del precio.
- Nunca amplía un stop ni sube un límite para que la operación quepa.
- Nunca pide claves, contraseñas ni datos de acceso a tu cuenta de MT5.

## Seguridad

- Educación y herramientas, no es asesoramiento financiero.
- Empieza en una cuenta demo de MT5. Pasar a real es decisión tuya.
- Los CFD con apalancamiento pueden hacerte perder más rápido de lo que
  parece; un gap en la apertura del domingo o tras un dato puede saltarse el
  stop. La pérdida máxima mostrada es la pérdida en el precio del stop.
- La simulación es con las sesiones recientes, no una predicción, y no incluye
  el spread ni el swap.
