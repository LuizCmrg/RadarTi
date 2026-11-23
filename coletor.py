import os
import re
import json
import platform
import subprocess
from urllib.parse import urljoin
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import urllib3
import requests
import pandas as pd
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ARQUIVO_EXCEL = "ativos.xlsx"
ARQUIVO_RESULTADO = "resultado.json"
ARQUIVO_STATUS = "status_cache.json"

USUARIO_RICOH = "admin"
SENHA_RICOH = "Web2023++"

IPS_SHIJIBIRA = {
    "172.27.32.72", "172.27.32.75", "172.27.32.76", "172.27.32.77",
    "172.27.32.74", "172.27.32.214", "172.27.32.209", "172.27.32.70",
    "172.27.32.71", "172.27.32.73", "172.27.22.5", "172.27.40.68",
    "172.27.32.220", "172.27.24.33", "172.27.32.219", "172.27.27.5",
    "172.27.32.243"
}


def _salvar_json_atomico(caminho: str, dados) -> None:
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=4)
    os.replace(tmp, caminho)


def ping_ip(ip: str) -> bool:
    if not ip or str(ip).strip().lower() in ("nan", "none", "", "sem ip"):
        return False

    param = "-n" if platform.system().lower() == "windows" else "-c"
    timeout_param = "-w" if platform.system().lower() == "windows" else "-W"
    timeout_val = "1000" if platform.system().lower() == "windows" else "1"

    try:
        res = subprocess.run(
            ["ping", param, "1", timeout_param, timeout_val, str(ip).strip()],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return res.returncode == 0
    except Exception:
        return False


def _tentar_login_ricoh(session: requests.Session, base_url: str) -> None:
    """Tenta autenticar no Web Image Monitor caso exista formulário de login."""
    if not USUARIO_RICOH or not SENHA_RICOH:
        return

    caminhos_login = [
        f"{base_url}/web/guest/pt/websys/webArch/auth.cgi",
        f"{base_url}/web/entry/pt/websys/webArch/auth.cgi",
        f"{base_url}/web/guest/br/websys/webArch/auth.cgi",
        f"{base_url}/web/entry/br/websys/webArch/auth.cgi",
        f"{base_url}/web/guest/en/websys/webArch/auth.cgi",
    ]

    for url_auth in caminhos_login:
        try:
            r = session.get(url_auth, timeout=4)
            if r.status_code != 200 or "password" not in r.text.lower():
                continue

            soup = BeautifulSoup(r.text, "html.parser")
            form = soup.find("form")
            action = urljoin(base_url, form.get("action")) if form and form.get("action") else url_auth

            payload = {}
            for inp in soup.find_all("input", {"name": True}):
                nome = inp.get("name")
                tipo = (inp.get("type") or "").lower()
                val = inp.get("value", "")
                nome_low = nome.lower()

                if tipo == "password" or any(k in nome_low for k in ("pass", "pwd", "senha")):
                    payload[nome] = SENHA_RICOH
                elif any(k in nome_low for k in ("user", "login", "id", "nome")):
                    payload[nome] = USUARIO_RICOH
                else:
                    payload[nome] = val

            if payload:
                session.post(action, data=payload, timeout=5, allow_redirects=True)
                return
        except Exception:
            continue


def _preparar_sessao_com_cookie(ip: str) -> tuple[requests.Session, str]:
    """
    Cria uma sessão HTTP/HTTPS e garante cookie de sessão e tentativa de login no WIM.
    """
    session = requests.Session()
    session.verify = False
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Connection": "keep-alive",
    })

    base_url = f"http://{ip}"
    for proto in ("http", "https"):
        try:
            r_root = session.get(f"{proto}://{ip}/", timeout=4)
            if r_root.status_code == 200:
                base_url = f"{proto}://{ip}"
                break
        except Exception:
            continue

    for lang in ("pt", "br", "en"):
        for mode in ("guest", "entry"):
            url_main = f"{base_url}/web/{mode}/{lang}/websys/webArch/mainFrame.cgi"
            try:
                resp = session.get(url_main, timeout=4)
                if resp.status_code == 200 and "MSG_COOKIEOFF" in resp.text:
                    soup = BeautifulSoup(resp.text, "html.parser")
                    form = soup.find("form", {"name": "form1"})
                    action = (
                        form.get("action", f"/web/{mode}/{lang}/websys/webArch/message.cgi")
                        if form else f"/web/{mode}/{lang}/websys/webArch/message.cgi"
                    )
                    post_url = urljoin(base_url, action)
                    dados_form = {
                        inp.get("name"): inp.get("value", "")
                        for inp in soup.find_all("input", {"name": True})
                    }
                    session.post(post_url, data=dados_form, timeout=4)
                    session.get(url_main, timeout=4)
                    _tentar_login_ricoh(session, base_url)
                    return session, base_url
                elif resp.status_code == 200:
                    _tentar_login_ricoh(session, base_url)
                    return session, base_url
            except Exception:
                continue

    _tentar_login_ricoh(session, base_url)
    return session, base_url


def _coletar_htmls_com_frames(url_inicial: str, session: requests.Session) -> list[str]:
    htmls = []
    try:
        resp = session.get(url_inicial, timeout=5)
        if resp.status_code != 200:
            return htmls
        htmls.append(resp.text)
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup.find_all(["frame", "iframe"], src=True):
            frame_url = urljoin(resp.url, tag["src"])
            try:
                r_frame = session.get(frame_url, timeout=5)
                if r_frame.status_code == 200:
                    htmls.append(r_frame.text)
            except Exception:
                continue
    except Exception:
        pass
    return htmls


def _extrair_numero_contador_html(html: str) -> int | None:
    if not html or "MSG_COOKIEOFF" in html:
        return None

    soup = BeautifulSoup(html, "html.parser")
    prioritarios = []
    secundarios = []

    for tr in soup.find_all("tr"):
        celulas = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)) for td in tr.find_all(["th", "td"])]
        if len(celulas) >= 2:
            rotulo = celulas[0].lower()
            if any(k in rotulo for k in ("contador total", "total counter")):
                for cel in celulas[1:]:
                    m_num = re.search(r"\b(\d[\d\.,]{1,12})\b", cel)
                    if m_num:
                        limpo = re.sub(r"[^\d]", "", m_num.group(1))
                        if limpo.isdigit():
                            prioritarios.append(int(limpo))
            elif "total" in rotulo and "subtotal" not in rotulo:
                for cel in celulas[1:]:
                    m_num = re.search(r"\b(\d[\d\.,]{1,12})\b", cel)
                    if m_num:
                        limpo = re.sub(r"[^\d]", "", m_num.group(1))
                        if limpo.isdigit():
                            secundarios.append(int(limpo))

    if prioritarios:
        return max(prioritarios)
    if secundarios:
        return max(secundarios)

    texto_pagina = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    padroes = [
        re.compile(r"Contador\s*(?:de\s*)?total\s*[:\-]?\s*([\d\.,]+)", re.IGNORECASE),
        re.compile(r"Total\s*counter\s*[:\-]?\s*([\d\.,]+)", re.IGNORECASE),
    ]
    for padrao in padroes:
        match = padrao.search(texto_pagina)
        if match:
            valor_limpo = re.sub(r"[^\d]", "", match.group(1))
            if valor_limpo.isdigit():
                return int(valor_limpo)

    return None


def _buscar_contador_nas_urls(base_url: str, session: requests.Session) -> int | None:
    urls_candidatas = [
        f"{base_url}/web/guest/pt/websys/status/getUniteCounter.cgi",
        f"{base_url}/web/entry/pt/websys/status/getUniteCounter.cgi",
        f"{base_url}/web/guest/br/websys/status/getUniteCounter.cgi",
        f"{base_url}/web/entry/br/websys/status/getUniteCounter.cgi",
        f"{base_url}/web/guest/pt/websys/status/getCounter.cgi",
        f"{base_url}/web/entry/pt/websys/status/getCounter.cgi",
        f"{base_url}/web/guest/br/websys/status/getCounter.cgi",
        f"{base_url}/web/guest/pt/websys/status/getUniteCurrent.cgi",
        f"{base_url}/web/guest/br/websys/status/getUniteCurrent.cgi",
        f"{base_url}/web/guest/pt/websys/status/configuration.cgi",
        f"{base_url}/web/guest/br/websys/status/configuration.cgi",
        f"{base_url}/web/guest/en/websys/status/getUniteCounter.cgi",
        f"{base_url}/web/guest/en/websys/status/getCounter.cgi",
    ]

    for url in urls_candidatas:
        htmls = _coletar_htmls_com_frames(url, session)
        for html in htmls:
            valor = _extrair_numero_contador_html(html)
            if valor is not None:
                return valor

    return None


def coletar_contador_ricoh(ip: str, session: requests.Session = None, base_url: str = None) -> dict:
    if session is None or base_url is None:
        session, base_url = _preparar_sessao_com_cookie(ip)

    contador = _buscar_contador_nas_urls(base_url, session)
    if contador is not None:
        return {"contador_total": contador, "contador_erro": None}

    return {"contador_total": None, "contador_erro": "Contador não encontrado no WIM"}


def checar_suprimentos_ricoh(
    ip: str,
    modelo: str = "",
    setor: str = "",
    session: requests.Session = None,
    base_url: str = None
) -> dict:
    if session is None or base_url is None:
        session, base_url = _preparar_sessao_com_cookie(ip)

    urls_status = [
        f"{base_url}/web/guest/pt/websys/webArch/topPage.cgi",
        f"{base_url}/web/entry/pt/websys/webArch/topPage.cgi",
        f"{base_url}/web/guest/pt/websys/webArch/getStatus.cgi",
        f"{base_url}/web/guest/br/websys/webArch/getStatus.cgi",
        f"{base_url}/web/guest/pt/websys/status/configuration.cgi",
        f"{base_url}/web/guest/br/websys/status/configuration.cgi",
    ]

    toners_encontrados = []
    alerta_toner = False
    papel_vazio = False
    papel_quase_vazio = False

    htmls_para_ler = []
    for u in urls_status:
        htmls_para_ler.extend(_coletar_htmls_com_frames(u, session))

    cores_mapa = [
        ("Preto", ["preto", "black", "toner k"]),
        ("Ciano", ["ciano", "cyan", "toner c"]),
        ("Magenta", ["magenta", "toner m"]),
        ("Amarelo", ["amarelo", "yellow", "toner y"]),
    ]

    for html in htmls_para_ler:
        if "MSG_COOKIEOFF" in html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        texto_lower = soup.get_text(" ", strip=True).lower()

        if any(p in texto_lower for p in ["sem papel", "papel esgotado", "abastecer papel", "carregar papel", "no paper", "paper end"]):
            papel_vazio = True
        elif any(p in texto_lower for p in ["pouco papel", "baixo papel", "paper near end", "low paper"]):
            papel_quase_vazio = True

        for nome_cor, aliases in cores_mapa:
            for alias in aliases:
                m = re.search(rf"{alias}\s*[:\-]?\s*(\d{{1,3}})\s*%", texto_lower)
                if m:
                    pct = int(m.group(1))
                    if not any(t.startswith(f"{nome_cor}:") for t in toners_encontrados):
                        toners_encontrados.append(f"{nome_cor}: {pct}%")
                        if pct <= 20:
                            alerta_toner = True
                    break

        if not toners_encontrados:
            for tr in soup.find_all("tr"):
                tr_txt = tr.get_text(" ", strip=True).lower()
                for nome_cor, aliases in cores_mapa:
                    if any(a in tr_txt for a in aliases):
                        img_bar = tr.find("img", width=True)
                        div_bar = tr.find(style=re.compile(r"width\s*:\s*\d+", re.I))
                        pct = None
                        if img_bar and str(img_bar["width"]).isdigit():
                            w = int(img_bar["width"])
                            pct = min(100, round((w / 160) * 100)) if w > 100 else w
                        elif div_bar:
                            m_w = re.search(r"width\s*:\s*(\d+)", div_bar["style"], re.I)
                            if m_w:
                                pct = min(100, int(m_w.group(1)))

                        if pct is not None and not any(t.startswith(f"{nome_cor}:") for t in toners_encontrados):
                            toners_encontrados.append(f"{nome_cor}: {pct}%")
                            if pct <= 20:
                                alerta_toner = True
                        elif ("esgotado" in tr_txt or "vazio" in tr_txt) and not any(t.startswith(f"{nome_cor}:") for t in toners_encontrados):
                            toners_encontrados.append(f"{nome_cor}: Esgotado")
                            alerta_toner = True
                        elif "quase" in tr_txt and not any(t.startswith(f"{nome_cor}:") for t in toners_encontrados):
                            toners_encontrados.append(f"{nome_cor}: Quase esgotado")
                            alerta_toner = True

        if toners_encontrados:
            break

    toner_final = " | ".join(toners_encontrados) if toners_encontrados else "Preto: OK"
    alerta_papel = papel_vazio or papel_quase_vazio

    return {
        "toner": toner_final,
        "alerta_toner": alerta_toner,
        "alerta_papel": alerta_papel,
        "papel_vazio": papel_vazio,
        "papel_quase_vazio": papel_quase_vazio,
    }


def _coletar_ativo_individual(registro: dict) -> dict:
    ip = str(registro.get("ip", "")).strip()
    marca = str(registro.get("marca", "")).strip()
    modelo = str(registro.get("modelo", "")).strip()
    setor = str(registro.get("setor", "")).strip()
    tipo = str(registro.get("tipo", "")).strip()

    online = ping_ip(ip)
    is_ricoh = "ricoh" in f"{marca} {tipo}".lower()

    toner = ""
    alerta_toner = False
    alerta_papel = False
    papel_vazio = False
    papel_quase_vazio = False
    contador_total = None
    contador_data = None
    contador_erro = None

    if online and is_ricoh:
        try:
            session, base_url = _preparar_sessao_com_cookie(ip)

            sup = checar_suprimentos_ricoh(ip, modelo, setor, session, base_url)
            toner = sup["toner"]
            alerta_toner = sup["alerta_toner"]
            alerta_papel = sup["alerta_papel"]
            papel_vazio = sup["papel_vazio"]
            papel_quase_vazio = sup["papel_quase_vazio"]

            cont = coletar_contador_ricoh(ip, session, base_url)
            contador_total = cont["contador_total"]
            contador_erro = cont["contador_erro"]

            if contador_total is not None:
                contador_data = datetime.now().strftime("%Y-%m-%d %H:%M")
        except Exception as e:
            contador_erro = str(e)

    shijibira = ip in IPS_SHIJIBIRA or bool(registro.get("shijibira", False))

    return {
        "setor": setor,
        "marca": marca,
        "modelo": modelo,
        "numero_serie": str(registro.get("numero_serie", "")).strip(),
        "ip": ip,
        "imp": str(registro.get("imp", "")).strip(),
        "site": str(registro.get("site", "SOR2")).strip(),
        "tipo": tipo,
        "versao": str(registro.get("versao", "")).strip(),
        "online": online,
        "critica": shijibira,
        "shijibira": shijibira,
        "toner": toner,
        "alerta_toner": alerta_toner,
        "alerta_papel": alerta_papel,
        "papel_vazio": papel_vazio,
        "papel_quase_vazio": papel_quase_vazio,
        "contador_total": contador_total,
        "contador_data": contador_data,
        "contador_erro": contador_erro,
    }


def _carregar_ativos_excel() -> list[dict]:
    if not os.path.exists(ARQUIVO_EXCEL):
        return []

    df = pd.read_excel(ARQUIVO_EXCEL).fillna("")
    df.columns = [str(c).strip().lower() for c in df.columns]

    ativos = []
    for _, row in df.iterrows():
        ativos.append({
            "setor": row.get("setor", row.get("local", "")),
            "marca": row.get("marca", ""),
            "modelo": row.get("modelo", ""),
            "numero_serie": row.get("numero_serie", row.get("serie", row.get("nº série", ""))),
            "ip": row.get("ip", ""),
            "imp": row.get("imp", ""),
            "site": row.get("site", row.get("planta", "SOR2")),
            "tipo": row.get("tipo", row.get("marca", "")),
            "versao": row.get("versao", ""),
        })
    return ativos


def executar_coleta() -> list[dict]:
    ativos = _carregar_ativos_excel()
    if not ativos:
        return []

    with ThreadPoolExecutor(max_workers=20) as executor:
        resultados = list(executor.map(_coletar_ativo_individual, ativos))

    _salvar_json_atomico(ARQUIVO_RESULTADO, resultados)
    _salvar_json_atomico(ARQUIVO_STATUS, resultados)

    return resultados


if __name__ == "__main__":
    dados = executar_coleta()
    print(f"Coleta concluída: {len(dados)} ativos.")