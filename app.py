import os
import json
import threading
import time
from datetime import datetime
from flask import Flask, render_template, jsonify, request
from coletor import executar_coleta, coletar_contador_ricoh, checar_suprimentos_ricoh

app = Flask(__name__)

ARQUIVO_RESULTADO = 'resultado.json'
ARQUIVO_ABASTECIMENTOS = 'abastecimentos.json'
ARQUIVO_HISTORICO_CONTADORES = 'historico_contadores.json'


def ler_json(caminho, padrao=None):
    if padrao is None:
        padrao = []
    if not os.path.exists(caminho):
        return padrao
    try:
        with open(caminho, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return padrao


def salvar_json(caminho, dados):
    tmp = caminho + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(dados, f, ensure_ascii=False, indent=4)
    os.replace(tmp, caminho)


def _parse_data_flexivel(valor):
    if not valor:
        return None
    valor = str(valor).strip()
    for fmt in ('%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M', '%d/%m/%Y', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(valor, fmt)
        except ValueError:
            continue
    return None


def registrar_snapshot_contador(ativo):
    if not isinstance(ativo, dict):
        return
    contador = ativo.get('contador_total')
    if contador is None or str(contador).strip() in ('', 'N/A', 'Indisponível'):
        return
    try:
        contador_num = int(str(contador).replace('.', '').strip())
    except ValueError:
        return

    agora_str = ativo.get('contador_data') or datetime.now().strftime('%d/%m/%Y %H:%M')
    historico = ler_json(ARQUIVO_HISTORICO_CONTADORES, [])
    ip = str(ativo.get('ip', '')).strip()

    ja_existe = any(
        str(h.get('ip', '')).strip() == ip
        and h.get('contador') == contador_num
        and str(h.get('data_hora', ''))[:10] == str(agora_str)[:10]
        for h in historico
    )
    if not ja_existe:
        historico.append({
            'ip': ip,
            'setor': ativo.get('setor') or 'Sem Setor',
            'marca': ativo.get('marca') or '',
            'modelo': ativo.get('modelo') or '',
            'serie': ativo.get('numero_serie') or ativo.get('serie') or 'N/A',
            'site': ativo.get('site') or 'SOR2',
            'contador': contador_num,
            'data_hora': agora_str
        })
        salvar_json(ARQUIVO_HISTORICO_CONTADORES, historico)


def _registrar_snapshots_da_lista(lista_ativos):
    for item in lista_ativos or []:
        registrar_snapshot_contador(item)


def loop_coleta_background():
    while True:
        try:
            dados = executar_coleta()
            _registrar_snapshots_da_lista(dados)
        except Exception as e:
            print(f"[!] Erro no loop de coleta: {e}")
        time.sleep(120)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    dados = ler_json(ARQUIVO_RESULTADO, [])
    _registrar_snapshots_da_lista(dados)
    return jsonify(dados)


@app.route('/api/atualizar', methods=['POST'])
def api_atualizar():
    dados = executar_coleta()
    _registrar_snapshots_da_lista(dados)
    return jsonify({'status': 'ok', 'total': len(dados)})


@app.route('/api/detalhes/<ip>')
def api_detalhes(ip):
    dados = ler_json(ARQUIVO_RESULTADO, [])
    for item in dados:
        if item.get('ip') == ip:
            return jsonify(item)
    return jsonify({'erro': 'Equipamento não encontrado'}), 404


@app.route('/api/abastecimentos')
def api_abastecimentos():
    return jsonify(ler_json(ARQUIVO_ABASTECIMENTOS, []))


@app.route('/api/abastecimento', methods=['POST'])
def api_abastecimento():
    payload = request.get_json(force=True)
    ip = str(payload.get('ip', '')).strip()
    setor = str(payload.get('setor', '')).strip()
    modelo = str(payload.get('modelo', '')).strip()
    serie = str(payload.get('serie', '')).strip()
    tipo = str(payload.get('tipo', 'ricoh')).strip()
    insumos = payload.get('insumos', [])
    tecnico = str(payload.get('tecnico', 'TI')).strip()
    descricao_tipo = str(payload.get('descricao_tipo', 'OFFICE')).strip()

    resultados_atuais = ler_json(ARQUIVO_RESULTADO, [])
    historico = ler_json(ARQUIVO_ABASTECIMENTOS, [])

    contador_anterior = payload.get('contador_atual', 'N/A')
    for reg in historico:
        if reg.get('ip') == ip and reg.get('contador_atual') not in (None, 'N/A', 'Indisponível'):
            contador_anterior = reg.get('contador_atual')
            break

    if contador_anterior in (None, '', 'N/A', 'Indisponível'):
        for item in resultados_atuais:
            if item.get('ip') == ip and item.get('contador_total') not in (None, 'N/A', 'Indisponível'):
                contador_anterior = item.get('contador_total')
                break

    contador_atual = contador_anterior
    toner_nivel = 'Indisponível'
    papel_status = 'OK'

    if 'ricoh' in tipo.lower() and ip:
        res_cont = coletar_contador_ricoh(ip)
        if res_cont.get('contador_total') is not None:
            contador_atual = res_cont['contador_total']

        sup = checar_suprimentos_ricoh(ip, modelo, setor)
        toner_nivel = sup.get('toner', 'Indisponível')
        if sup.get('papel_vazio'):
            papel_status = 'Sem Papel'
        elif sup.get('papel_quase_vazio'):
            papel_status = 'Pouco Papel'
        else:
            papel_status = 'OK'

    delta_paginas = None
    try:
        if str(contador_anterior).isdigit() and str(contador_atual).isdigit():
            diff = int(contador_atual) - int(contador_anterior)
            if diff >= 0:
                delta_paginas = diff
    except Exception:
        delta_paginas = None

    agora_fmt = datetime.now().strftime('%d/%m/%Y %H:%M')

    registro = {
        'ip': ip,
        'setor': setor,
        'modelo': modelo,
        'serie': serie,
        'descricao_tipo': descricao_tipo,
        'insumos_trocados': insumos,
        'contador_anterior': contador_anterior,
        'contador_atual': contador_atual,
        'contador_momento': contador_atual,
        'delta_paginas': delta_paginas,
        'toner_nivel': toner_nivel,
        'papel_status': papel_status,
        'tecnico': tecnico,
        'data_hora': agora_fmt
    }

    historico.insert(0, registro)
    salvar_json(ARQUIVO_ABASTECIMENTOS, historico)

    for item in resultados_atuais:
        if item.get('ip') == ip:
            if contador_atual not in (None, 'N/A', 'Indisponível'):
                item['contador_total'] = contador_atual
                item['contador_data'] = agora_fmt
            if toner_nivel != 'Indisponível':
                item['toner'] = toner_nivel
            registrar_snapshot_contador(item)
            break
    salvar_json(ARQUIVO_RESULTADO, resultados_atuais)

    return jsonify({
        'status': 'sucesso',
        'contador_anterior': contador_anterior,
        'contador_coletado': contador_atual,
        'delta_paginas': delta_paginas,
        'toner_nivel': toner_nivel,
        'papel_status': papel_status
    })


@app.route('/api/relatorio-contadores')
def api_relatorio_contadores():
    dt_inicio = _parse_data_flexivel(request.args.get('data_inicio', ''))
    dt_fim = _parse_data_flexivel(request.args.get('data_fim', ''))
    if dt_fim and len(str(request.args.get('data_fim', '')).strip()) == 10:
        dt_fim = dt_fim.replace(hour=23, minute=59, second=59)

    filtro_setor = (request.args.get('setor') or '').strip().lower()
    filtro_marca = (request.args.get('marca') or 'todas').strip().lower()
    filtro_planta = (request.args.get('planta') or 'todas').strip().lower()

    _registrar_snapshots_da_lista(ler_json(ARQUIVO_RESULTADO, []))
    historico = ler_json(ARQUIVO_HISTORICO_CONTADORES, [])

    filtrados = []
    for item in historico:
        dt_item = _parse_data_flexivel(item.get('data_hora', ''))
        if dt_inicio and dt_item and dt_item < dt_inicio:
            continue
        if dt_fim and dt_item and dt_item > dt_fim:
            continue

        setor_item = (item.get('setor') or 'Sem Setor').strip()
        if filtro_setor and filtro_setor != 'todos' and filtro_setor not in setor_item.lower():
            continue

        marca_item = (item.get('marca') or '').lower()
        if filtro_marca != 'todas' and filtro_marca not in marca_item:
            continue

        site_item = (item.get('site') or 'SOR2').lower()
        if filtro_planta != 'todas' and filtro_planta not in site_item:
            continue

        filtrados.append({
            **item,
            '_dt': dt_item.isoformat() if dt_item else ''
        })

    por_ip = {}
    for r in filtrados:
        por_ip.setdefault(r.get('ip', ''), []).append(r)

    detalhes = []
    resumo_setor = {}
    resumo_data = {}

    for ip, leituras in por_ip.items():
        if not ip:
            continue
        leituras_ordenadas = sorted(leituras, key=lambda x: x.get('_dt') or '')
        primeira = leituras_ordenadas[0]
        ultima = leituras_ordenadas[-1]
        c_inicial = int(primeira.get('contador') or 0)
        c_final = int(ultima.get('contador') or 0)
        delta = (c_final - c_inicial) if len(leituras_ordenadas) > 1 and c_final >= c_inicial else 0

        setor = ultima.get('setor') or 'Sem Setor'
        detalhes.append({
            'ip': ip,
            'setor': setor,
            'marca': ultima.get('marca') or '',
            'modelo': ultima.get('modelo') or '',
            'numero_serie': ultima.get('serie') or 'N/A',
            'site': ultima.get('site') or 'SOR2',
            'contador_inicial': c_inicial,
            'contador_final': c_final,
            'paginas_periodo': delta,
            'leituras_qtd': len(leituras_ordenadas),
            'ultima_coleta': ultima.get('data_hora') or '-'
        })

        if setor not in resumo_setor:
            resumo_setor[setor] = {
                'setor': setor,
                'equipamentos': 0,
                'paginas_periodo': 0,
                'contador_acumulado': 0
            }
        resumo_setor[setor]['equipamentos'] += 1
        resumo_setor[setor]['paginas_periodo'] += delta
        resumo_setor[setor]['contador_acumulado'] += c_final

        dia_chave = str(ultima.get('data_hora') or '')[:10] or 'Atual'
        resumo_data[dia_chave] = resumo_data.get(dia_chave, 0) + delta

    setores_ordenados = sorted(resumo_setor.values(), key=lambda x: x['paginas_periodo'], reverse=True)
    total_paginas = sum(x['paginas_periodo'] for x in detalhes)
    total_equipamentos = len(detalhes)
    media_por_equip = round(total_paginas / total_equipamentos) if total_equipamentos else 0
    top_setor = setores_ordenados[0]['setor'] if setores_ordenados else 'N/A'

    return jsonify({
        'kpis': {
            'total_paginas_periodo': total_paginas,
            'equipamentos_avaliados': total_equipamentos,
            'media_por_equipamento': media_por_equip,
            'setor_maior_volume': top_setor
        },
        'por_setor': setores_ordenados,
        'por_data': [{'data': k, 'paginas': v} for k, v in resumo_data.items()],
        'detalhes': detalhes
    })


if __name__ == '__main__':
    t = threading.Thread(target=loop_coleta_background, daemon=True)
    t.start()
    app.run(host='0.0.0.0', port=5000, debug=False)