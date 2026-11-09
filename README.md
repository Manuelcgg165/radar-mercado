# Radar de mercado

Script semanal que vigila unos 160 valores de chips e IA, defensa, espacio, telecomunicaciones y satélites, energía y red eléctrica y materias primas críticas, y señala los movimientos anómalos de la semana para el parte de los domingos.

Es un radar de anomalías, no una recomendación: cada señal hay que explicarla antes de convertirla en tesis.

## Cuándo se ejecuta

GitHub Actions lo lanza los sábados a las 07:00 UTC y cada vez que cambian `universo.csv` o `radar.py`. Para lanzarlo a mano: pestaña **Actions** > **Radar de mercado** > **Run workflow**.

## Señales

| Señal | Condición | Peso |
|---|---|---|
| Compras de directivos | Directivos o consejeros de empresas de EE. UU. compran en mercado al menos 100.000 $ en la semana (formulario 4 de la SEC) | 3, o 4 si son dos o más |
| Volumen anómalo | Volumen medio de la semana ≥ 2 veces el de las 50 sesiones anteriores, o un día ≥ 4 veces la mediana, con un movimiento frente a la referencia de al menos 5 pp y 1,5 desviaciones típicas | 2 |
| Ruptura de fuerza relativa | El cociente valor/referencia marca máximo de 6 meses y el valor le saca al menos 15 pp y 1,5 desviaciones típicas a 3 meses | 2 |
| Máximo de 52 semanas | Cierre por encima de todos los del año anterior, y mejor que su referencia en el último mes | 1 |
| Hundimiento de fuerza relativa | Como la ruptura, a la baja | 1 |
| Mínimo de 52 semanas | Cierre por debajo de todos los del año anterior, y peor que su referencia en el último mes | 1 |

Si una señal se repite varias semanas seguidas, su peso se divide entre el número de semanas, para que suba lo nuevo. `radar_mercado.csv` recoge las 12 filas de mayor puntuación.

## Ficheros

- `universo.csv`: valores (ticker de Yahoo Finance), nombre, tema y referencia. Si la referencia está vacía, el valor se compara con la media equiponderada de los demás valores del mismo tema sin referencia (cesta de pares).
- `salida/radar_mercado.csv`: señales de la semana.
- `salida/radar_universo.csv`: métricas de todos los valores.
- `salida/estado.json`: fecha de los datos y lo que ha fallado.
- `salida/historial/`: una copia por semana, para evaluar más adelante si las señales anticipaban algo.

Columnas: `ret_5d` y `mom_12_1` en %; `rel_*` en puntos porcentuales frente a la referencia; todo en euros. `mom_12_1` es la rentabilidad de 12 meses sin el último. `vol_semana_x` y `vol_pico_x` son múltiplos del volumen normal. `dist_max52` es la distancia en % al máximo de 52 semanas, en la divisa de cotización.

## Cambiar el universo o los umbrales

- **Universo:** edita `universo.csv` desde GitHub (icono del lápiz). El ticker lleva el sufijo de Yahoo: `.MC` Madrid, `.DE` Xetra, `.PA` París, `.MI` Milán, `.AS` Ámsterdam, `.BR` Bruselas, `.L` Londres, `.ST` Estocolmo, `.OL` Oslo, `.CO` Copenhague, `.HE` Helsinki, `.SW` Suiza, `.TO` Toronto, `.T` Tokio, `.KS` Corea, `.AX` Australia; sin sufijo, EE. UU.
- **Umbrales:** bloque *Configuración* al principio de `radar.py`.

Cada cambio lanza una ejecución. Si un ticker no existe, aparece en `estado.json`, en `sin_datos`.

Opcional: la SEC pide identificarse con un nombre y un correo de contacto. Puedes crear el secreto `SEC_USER_AGENT` en *Settings > Secrets and variables > Actions* (por ejemplo, `Radar personal tu@correo.com`); si no existe, el script usa uno genérico.

## Límites

- Yahoo Finance no es una fuente oficial: puede fallar o cambiar sin aviso. Si falla más de la mitad del universo, el script no actualiza nada y la ejecución sale en rojo.
- Las compras de directivos solo cubren empresas de EE. UU. Las europeas (CNMV, BaFin, AMF) no tienen una fuente gratuita unificada.
- Los datos de cada valor son los de su bolsa principal; el volumen de las acciones europeas no incluye otras plataformas de negociación.
