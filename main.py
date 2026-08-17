import pandas as pd
import numpy as np
import requests
import os
import json
import numbers
import gspread

from datetime import datetime
from zoneinfo import ZoneInfo
from oauth2client.service_account import ServiceAccountCredentials


# ============================================================
# CONFIGURAÇÕES
# ============================================================

ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN")
IG_ACCOUNT_ID = os.environ.get("META_IG_ACCOUNT_ID")
GCP_CREDENTIALS = os.environ.get("GCP_CREDENTIALS")
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")

# Deixamos a versão da API configurável.
# Atualizar aqui ou no GitHub Secrets sem precisar alterar o código.
META_API_VERSION = os.environ.get(
    "META_API_VERSION",
    "v25.0"
)

META_GRAPH_HOST = os.environ.get(
    "META_GRAPH_HOST",
    "https://graph.facebook.com"
)

TIMEZONE = "America/Sao_Paulo"


# ============================================================
# FUNÇÕES AUXILIARES
# ============================================================

def normalizar_texto(serie):
    """
    Padroniza textos usados nas chaves de relacionamento.
    """

    if serie is None:
        return serie

    serie = (
        serie
        .fillna("")
        .astype(str)
        .str.strip()
        .str.lower()
    )

    serie = serie.replace({
        "(vazio)": "",
        "nan": "",
        "none": "",
        "null": "",
    })

    return serie


def converter_monetario(valor):
    """
    Converte valores monetários de forma robusta.

    Aceita:
        35.99
        35,99
        R$ 35,99
        1.234,56
        1,234.56

    Importante:
    quando o valor já é numérico, NÃO faz nenhuma transformação.
    """

    if pd.isna(valor) or valor == "":
        return 0.0

    if isinstance(valor, numbers.Number):
        return float(valor)

    texto = str(valor).strip()

    if not texto:
        return 0.0

    texto = (
        texto
        .replace("R$", "")
        .replace("\xa0", "")
        .strip()
    )

    if not texto:
        return 0.0

    # Brasileiro/internacional com milhar + decimal
    if "," in texto and "." in texto:

        # 1.234,56
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(".", "")
            texto = texto.replace(",", ".")

        # 1,234.56
        else:
            texto = texto.replace(",", "")

    # Exemplo 35,99
    elif "," in texto:
        texto = texto.replace(",", ".")

    # Se tiver somente ponto, mantemos.
    # 1.077 continua 1.077
    # 35.99 continua 35.99

    try:
        return float(texto)

    except ValueError:
        print(f"[AVISO] Valor monetário inválido: {valor}")
        return 0.0


def requisicao_meta(url):
    """
    Executa requisição na Meta API e retorna:
        response_json, erro

    Se estiver tudo certo:
        response_json = JSON
        erro = ""

    Se houver erro:
        response_json = {}
        erro = descrição
    """

    try:

        response = requests.get(
            url,
            timeout=30
        )

        try:
            data = response.json()
        except ValueError:
            data = {}

        if response.status_code != 200:

            erro = (
                f"HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )

            return {}, erro

        if "error" in data:

            erro = str(data["error"])

            return {}, erro

        return data, ""

    except requests.RequestException as e:

        return {}, f"RequestException: {e}"

    except Exception as e:

        return {}, f"Exception: {e}"


def extrair_metricas_insights(data):
    """
    Extrai as métricas retornadas pelo endpoint /insights.
    """

    metricas = {
        "views": np.nan,
        "likes": np.nan,
        "comments": np.nan,
        "saved": np.nan,
        "shares": np.nan,
    }

    for item in data.get("data", []):

        nome = item.get("name")

        if nome not in metricas:
            continue

        valores = item.get("values", [])

        if valores:

            valor = valores[0].get("value")

            if valor is not None:
                metricas[nome] = valor

    return metricas


def obter_dados_instagram(post_id):
    """
    Busca:

    1. Metadados do post
    2. Insights do post

    Retorna um dicionário completo.
    """

    resultado = {
        "Post_ID": str(post_id),

        "Visualizacoes": np.nan,
        "Curtidas": np.nan,
        "Comentarios": np.nan,
        "Salvamentos": np.nan,
        "Compartilhamentos": np.nan,

        "Data_Publicacao": "",
        "Hora_Publicacao": "",
        "Dia_Semana": "",
        "Mes": "",
        "Ano_Mes": "",

        "Media_Type_API": "",
        "Tipo_Conteudo_API": "",

        "Meta_Status": "ERRO",
        "Meta_Erro": "",
    }

    # ========================================================
    # METADADOS DO POST
    # ========================================================

    url_media = (
        f"{META_GRAPH_HOST}/{META_API_VERSION}/{post_id}"
        f"?fields=timestamp,media_type,media_product_type"
        f"&access_token={ACCESS_TOKEN}"
    )

    media_data, media_erro = requisicao_meta(url_media)

    if media_erro:

        resultado["Meta_Erro"] = (
            f"Metadata: {media_erro}"
        )

    else:

        timestamp = media_data.get("timestamp", "")

        if timestamp:

            try:

                dt_utc = pd.to_datetime(
                    timestamp,
                    utc=True
                )

                dt_br = dt_utc.tz_convert(
                    TIMEZONE
                )

                resultado["Data_Publicacao"] = (
                    dt_br.strftime("%Y-%m-%d")
                )

                resultado["Hora_Publicacao"] = (
                    dt_br.strftime("%H:%M")
                )

                dias = {
                    0: "segunda-feira",
                    1: "terça-feira",
                    2: "quarta-feira",
                    3: "quinta-feira",
                    4: "sexta-feira",
                    5: "sábado",
                    6: "domingo",
                }

                resultado["Dia_Semana"] = dias[
                    dt_br.weekday()
                ]

                resultado["Mes"] = (
                    dt_br.strftime("%m")
                )

                resultado["Ano_Mes"] = (
                    dt_br.strftime("%Y-%m")
                )

            except Exception as e:

                resultado["Meta_Erro"] = (
                    f"Erro timestamp: {e}"
                )

        resultado["Media_Type_API"] = (
            media_data.get("media_type", "")
        )

        resultado["Tipo_Conteudo_API"] = (
            media_data.get("media_product_type", "")
        )

    # ========================================================
    # INSIGHTS
    # ========================================================

    url_insights = (
        f"{META_GRAPH_HOST}/"
        f"{META_API_VERSION}/"
        f"{post_id}/insights"
        f"?metric=views,likes,comments,saved,shares"
        f"&access_token={ACCESS_TOKEN}"
    )

    insights_data, insights_erro = requisicao_meta(
        url_insights
    )

    if insights_erro:

        # Não transformamos erro em zero.
        # Guardamos o erro para diagnóstico.
        resultado["Meta_Status"] = "ERRO"

        erro_atual = resultado.get(
            "Meta_Erro",
            ""
        )

        if erro_atual:
            resultado["Meta_Erro"] += (
                f" | Insights: {insights_erro}"
            )
        else:
            resultado["Meta_Erro"] = (
                f"Insights: {insights_erro}"
            )

        return resultado

    metricas = extrair_metricas_insights(
        insights_data
    )

    resultado["Visualizacoes"] = metricas["views"]
    resultado["Curtidas"] = metricas["likes"]
    resultado["Comentarios"] = metricas["comments"]
    resultado["Salvamentos"] = metricas["saved"]
    resultado["Compartilhamentos"] = metricas["shares"]

    resultado["Meta_Status"] = "OK"
    resultado["Meta_Erro"] = ""

    return resultado


def extrair_subs_clique(sub_str):
    """
    Espera algo como:

        cama-carrosel
        moveis-carrossel

    Retorna sub_id1 e sub_id2.
    """

    texto = str(sub_str or "").strip()

    partes = [
        p.strip().lower()
        for p in texto.split("-")
        if p.strip()
    ]

    sub1 = (
        partes[0]
        if len(partes) >= 1
        else ""
    )

    sub2 = (
        partes[1]
        if len(partes) >= 2
        else ""
    )

    return pd.Series(
        [
            sub1,
            sub2
        ]
    )


def nome_tipo_conteudo(tipo):
    """
    Traduz alguns tipos da API para nomes mais amigáveis.
    """

    mapa = {
        "FEED": "Feed",
        "REELS": "Reels",
        "STORY": "Story",
        "CAROUSEL_ALBUM": "Carrossel",
        "AD": "Anúncio",
    }

    return mapa.get(
        str(tipo).upper(),
        tipo
    )


def escrever_aba(planilha, nome_aba, dataframe):
    """
    Limpa a aba e escreve o DataFrame.
    """

    try:
        aba = planilha.worksheet(nome_aba)

    except gspread.WorksheetNotFound:

        aba = planilha.add_worksheet(
            title=nome_aba,
            rows=max(len(dataframe) + 10, 100),
            cols=max(len(dataframe.columns) + 5, 20)
        )

    aba.clear()

    valores = (
        [dataframe.columns.tolist()]
        + dataframe.values.tolist()
    )

    aba.update(
        valores,
        value_input_option="USER_ENTERED"
    )


# ============================================================
# PIPELINE PRINCIPAL
# ============================================================

def run_pipeline():

    print("=" * 70)
    print("INICIANDO PIPELINE")
    print("=" * 70)

    # ========================================================
    # VALIDAÇÃO DAS CREDENCIAIS
    # ========================================================

    obrigatorias = {
        "META_ACCESS_TOKEN": ACCESS_TOKEN,
        "GCP_CREDENTIALS": GCP_CREDENTIALS,
        "GOOGLE_SHEET_ID": SHEET_ID,
    }

    faltantes = [
        nome
        for nome, valor in obrigatorias.items()
        if not valor
    ]

    if faltantes:

        raise ValueError(
            "Variáveis de ambiente ausentes: "
            + ", ".join(faltantes)
        )

    # ========================================================
    # GOOGLE SHEETS
    # ========================================================

    print("Autenticando no Google Sheets...")

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]

    creds_dict = json.loads(
        GCP_CREDENTIALS
    )

    creds = (
        ServiceAccountCredentials
        .from_json_keyfile_dict(
            creds_dict,
            scope
        )
    )

    client = gspread.authorize(creds)

    planilha = client.open_by_key(
        SHEET_ID
    )

    # ========================================================
    # LEITURA DAS ABAS
    # ========================================================

    print("Lendo Tabela_Semente...")

    df_semente = pd.DataFrame(
        planilha
        .worksheet("Tabela_Semente")
        .get_all_records()
    )

    print("Lendo Shopee_Cliques...")

    df_cliques = pd.DataFrame(
        planilha
        .worksheet("Shopee_Cliques")
        .get_all_records()
    )

    print("Lendo Shopee_Vendas...")

    # UNFORMATTED_VALUE evita que formatação visual
    # de moeda do Sheets seja interpretada de maneira
    # inconsistente.
    df_vendas = pd.DataFrame(
        planilha
        .worksheet("Shopee_Vendas")
        .get_all_records(
            value_render_option="UNFORMATTED_VALUE"
        )
    )

    if df_semente.empty:

        print(
            "Tabela Semente vazia. "
            "Encerrando."
        )

        return

    # ========================================================
    # PADRONIZAÇÃO DA SEMENTE
    # ========================================================

    print("Padronizando Tabela_Semente...")

    df_semente["Post_ID"] = (
        df_semente["Post_ID"]
        .astype(str)
        .str.strip()
    )

    df_semente["sub_id1"] = normalizar_texto(
        df_semente["sub_id1"]
    )

    df_semente["sub_id2"] = normalizar_texto(
        df_semente["sub_id2"]
    )

    # Padroniza grafias comuns
    df_semente["sub_id2"] = (
        df_semente["sub_id2"]
        .replace({
            "carrosel": "carrossel"
        })
    )

    # ========================================================
    # DIAGNÓSTICO DE SUB_ID
    # ========================================================

    contagem_posts_subid = (
        df_semente
        .groupby(
            ["sub_id1", "sub_id2"],
            dropna=False
        )["Post_ID"]
        .nunique()
        .reset_index(
            name="Posts_Com_Mesmo_SubID"
        )
    )

    df_semente = pd.merge(
        df_semente,
        contagem_posts_subid,
        on=[
            "sub_id1",
            "sub_id2"
        ],
        how="left"
    )

    df_semente["SubID_Unico_No_Post"] = (
        df_semente["Posts_Com_Mesmo_SubID"] == 1
    )

    df_semente["Alerta_Atribuicao"] = np.where(
        df_semente["SubID_Unico_No_Post"],
        "OK",
        "SUB_ID_REPETIDO"
    )

    # ========================================================
    # INSTAGRAM / META
    # ========================================================

    print("=" * 70)
    print("EXTRAINDO DADOS DO INSTAGRAM")
    print(
        f"API: {META_API_VERSION}"
    )
    print("=" * 70)

    dados_ig = []

    total_posts = len(
        df_semente["Post_ID"]
    )

    for numero, post_id in enumerate(
        df_semente["Post_ID"],
        start=1
    ):

        if (
            not post_id
            or post_id.lower() == "nan"
        ):
            continue

        print(
            f"[Instagram {numero}/{total_posts}] "
            f"{post_id}"
        )

        resultado = obter_dados_instagram(
            post_id
        )

        dados_ig.append(
            resultado
        )

    df_meta = pd.DataFrame(
        dados_ig
    )

    # ========================================================
    # MERGE INSTAGRAM + SEMENTE
    # ========================================================

    df_analise = pd.merge(
        df_semente,
        df_meta,
        on="Post_ID",
        how="left",
        validate="one_to_one"
    )

    # ========================================================
    # TIPO DE CONTEÚDO
    # ========================================================

    if "Tipo de Conteúdo" in df_analise.columns:

        df_analise["Tipo_Conteudo"] = (
            df_analise["Tipo de Conteúdo"]
            .fillna(
                df_analise["Tipo_Conteudo_API"]
            )
        )

    else:

        df_analise["Tipo_Conteudo"] = (
            df_analise["Tipo_Conteudo_API"]
            .apply(nome_tipo_conteudo)
        )

    # ========================================================
    # CLIQUES SHOPEE
    # ========================================================

    print("=" * 70)
    print("PROCESSANDO CLIQUES SHOPEE")
    print("=" * 70)

    if df_cliques.empty:

        shopee_cliques_agrupado = pd.DataFrame(
            columns=[
                "sub_id1",
                "sub_id2",
                "Cliques_Shopee"
            ]
        )

    else:

        df_cliques["Sub_id"] = (
            df_cliques["Sub_id"]
            .fillna("")
            .astype(str)
            .str.strip()
        )

        df_cliques[
            [
                "sub_id1",
                "sub_id2"
            ]
        ] = (
            df_cliques["Sub_id"]
            .apply(extrair_subs_clique)
        )

        df_cliques["sub_id1"] = (
            normalizar_texto(
                df_cliques["sub_id1"]
            )
        )

        df_cliques["sub_id2"] = (
            normalizar_texto(
                df_cliques["sub_id2"]
            )
        )

        df_cliques["sub_id2"] = (
            df_cliques["sub_id2"]
            .replace({
                "carrosel": "carrossel"
            })
        )

        shopee_cliques_agrupado = (
            df_cliques
            .groupby(
                [
                    "sub_id1",
                    "sub_id2"
                ],
                dropna=False
            )
            .size()
            .reset_index(
                name="Cliques_Shopee"
            )
        )

    # ========================================================
    # VENDAS SHOPEE
    # ========================================================

    print("=" * 70)
    print("PROCESSANDO VENDAS SHOPEE")
    print("=" * 70)

    if df_vendas.empty:

        raise ValueError(
            "Shopee_Vendas está vazia."
        )

    # --------------------------------------------------------
    # SUB IDS
    # --------------------------------------------------------

    df_vendas["sub_id1"] = (
        normalizar_texto(
            df_vendas["Sub_id1"]
        )
    )

    df_vendas["sub_id2"] = (
        normalizar_texto(
            df_vendas["Sub_id2"]
        )
    )

    df_vendas["sub_id2"] = (
        df_vendas["sub_id2"]
        .replace({
            "carrosel": "carrossel"
        })
    )

    # --------------------------------------------------------
    # CAMPOS FINANCEIROS
    # --------------------------------------------------------

    df_vendas[
        "Valor de Compra(R$)"
    ] = (
        df_vendas[
            "Valor de Compra(R$)"
        ]
        .apply(
            converter_monetario
        )
    )

    df_vendas[
        "Comissão líquida do afiliado(R$)"
    ] = (
        df_vendas[
            "Comissão líquida do afiliado(R$)"
        ]
        .apply(
            converter_monetario
        )
    )

    # --------------------------------------------------------
    # ID DO PEDIDO
    # --------------------------------------------------------

    df_vendas["ID do pedido"] = (
        df_vendas["ID do pedido"]
        .astype(str)
        .str.strip()
    )

    # --------------------------------------------------------
    # AGREGADO POR SUB_ID
    # --------------------------------------------------------

    shopee_vendas_agrupado = (
        df_vendas
        .groupby(
            [
                "sub_id1",
                "sub_id2"
            ],
            dropna=False
        )
        .agg(
            Compras_Shopee=(
                "ID do pedido",
                "nunique"
            ),

            Valor_Total_Compras=(
                "Valor de Compra(R$)",
                "sum"
            ),

            Comissao_Gerada=(
                "Comissão líquida do afiliado(R$)",
                "sum"
            )
        )
        .reset_index()
    )

    # ========================================================
    # VALOR POR PEDIDO
    # ========================================================

    valor_por_pedido = (
        df_vendas
        .groupby(
            [
                "sub_id1",
                "sub_id2",
                "ID do pedido"
            ],
            dropna=False
        )["Valor de Compra(R$)"]
        .sum()
        .reset_index(
            name="Valor_Pedido"
        )
    )

    # ========================================================
    # ESTATÍSTICAS DE TICKET
    # ========================================================

    ticket_estatisticas = (
        valor_por_pedido
        .groupby(
            [
                "sub_id1",
                "sub_id2"
            ],
            dropna=False
        )["Valor_Pedido"]
        .agg(
            Ticket_Medio="mean",
            Ticket_Mediano="median",
            Ticket_P25=lambda x: x.quantile(0.25),
            Ticket_P75=lambda x: x.quantile(0.75),
            Maior_Pedido="max",
            Menor_Pedido="min"
        )
        .reset_index()
    )

    # ========================================================
    # CONSOLIDADO SHOPEE
    # ========================================================

    shopee_consolidado = pd.merge(
        shopee_cliques_agrupado,
        shopee_vendas_agrupado,
        on=[
            "sub_id1",
            "sub_id2"
        ],
        how="outer"
    )

    shopee_consolidado = pd.merge(
        shopee_consolidado,
        ticket_estatisticas,
        on=[
            "sub_id1",
            "sub_id2"
        ],
        how="outer"
    )

    # ========================================================
    # MERGE COM INSTAGRAM
    # ========================================================

    df_analise = pd.merge(
        df_analise,
        shopee_consolidado,
        on=[
            "sub_id1",
            "sub_id2"
        ],
        how="left"
    )

    # ========================================================
    # MÉTRICAS BASE
    # ========================================================

    colunas_numericas_base = [
        "Visualizacoes",
        "Curtidas",
        "Comentarios",
        "Salvamentos",
        "Compartilhamentos",
        "Cliques_Shopee",
        "Compras_Shopee",
        "Valor_Total_Compras",
        "Comissao_Gerada",
        "Ticket_Medio",
        "Ticket_Mediano",
        "Ticket_P25",
        "Ticket_P75",
        "Maior_Pedido",
        "Menor_Pedido",
    ]

    for coluna in colunas_numericas_base:

        if coluna not in df_analise.columns:
            df_analise[coluna] = np.nan

    # As métricas Shopee sem venda são efetivamente zero.
    colunas_shopee_zero = [
        "Cliques_Shopee",
        "Compras_Shopee",
        "Valor_Total_Compras",
        "Comissao_Gerada",
    ]

    df_analise[
        colunas_shopee_zero
    ] = (
        df_analise[
            colunas_shopee_zero
        ]
        .fillna(0)
    )

    # ========================================================
    # INSTAGRAM: INTERAÇÕES
    # ========================================================

    # Apenas métricas existentes entram na soma.
    interacoes = (
        df_analise["Curtidas"].fillna(0)
        + df_analise["Comentarios"].fillna(0)
        + df_analise["Salvamentos"].fillna(0)
        + df_analise["Compartilhamentos"].fillna(0)
    )

    df_analise[
        "Interacoes"
    ] = interacoes

    # ========================================================
    # 1. TAXA DE ENGAJAMENTO
    # ========================================================

    df_analise[
        "Taxa_Engajamento(%)"
    ] = np.where(
        df_analise["Visualizacoes"] > 0,
        (
            df_analise["Interacoes"]
            / df_analise["Visualizacoes"]
        ) * 100,
        np.nan
    )

    # ========================================================
    # 2. INTERAÇÕES POR 1.000 VIEWS
    # ========================================================

    df_analise[
        "Interacoes_1k_Views"
    ] = np.where(
        df_analise["Visualizacoes"] > 0,
        (
            df_analise["Interacoes"]
            / df_analise["Visualizacoes"]
        ) * 1000,
        np.nan
    )

    # ========================================================
    # 3. SALVAMENTOS POR 1.000 VIEWS
    # ========================================================

    df_analise[
        "Salvamentos_1k_Views"
    ] = np.where(
        df_analise["Visualizacoes"] > 0,
        (
            df_analise["Salvamentos"].fillna(0)
            / df_analise["Visualizacoes"]
        ) * 1000,
        np.nan
    )

    # ========================================================
    # 4. COMPARTILHAMENTOS POR 1.000 VIEWS
    # ========================================================

    df_analise[
        "Compartilhamentos_1k_Views"
    ] = np.where(
        df_analise["Visualizacoes"] > 0,
        (
            df_analise["Compartilhamentos"].fillna(0)
            / df_analise["Visualizacoes"]
        ) * 1000,
        np.nan
    )

    # ========================================================
    # 5. CURTIDAS POR 1.000 VIEWS
    # ========================================================

    df_analise[
        "Curtidas_1k_Views"
    ] = np.where(
        df_analise["Visualizacoes"] > 0,
        (
            df_analise["Curtidas"].fillna(0)
            / df_analise["Visualizacoes"]
        ) * 1000,
        np.nan
    )

    # ========================================================
    # 6. CTR INSTAGRAM -> SHOPEE
    # ========================================================

    df_analise[
        "CTR_Shopee(%)"
    ] = np.where(
        df_analise["Visualizacoes"] > 0,
        (
            df_analise["Cliques_Shopee"]
            / df_analise["Visualizacoes"]
        ) * 100,
        np.nan
    )

    # ========================================================
    # 7. CONVERSÃO CLIQUE -> PEDIDO
    # ========================================================

    df_analise[
        "Conversao_Clique_Pedido(%)"
    ] = np.where(
        df_analise["Cliques_Shopee"] > 0,
        (
            df_analise["Compras_Shopee"]
            / df_analise["Cliques_Shopee"]
        ) * 100,
        np.nan
    )

    # ========================================================
    # 8. CONVERSÃO VIEW -> PEDIDO
    # ========================================================

    df_analise[
        "Conversao_View_Pedido(%)"
    ] = np.where(
        df_analise["Visualizacoes"] > 0,
        (
            df_analise["Compras_Shopee"]
            / df_analise["Visualizacoes"]
        ) * 100,
        np.nan
    )

    # ========================================================
    # 9. PEDIDOS POR 1.000 VIEWS
    # ========================================================

    df_analise[
        "Pedidos_1k_Views"
    ] = np.where(
        df_analise["Visualizacoes"] > 0,
        (
            df_analise["Compras_Shopee"]
            / df_analise["Visualizacoes"]
        ) * 1000,
        np.nan
    )

    # ========================================================
    # 10. GMV POR CLIQUE
    # ========================================================

    df_analise[
        "GMV_por_Clique(R$)"
    ] = np.where(
        df_analise["Cliques_Shopee"] > 0,
        (
            df_analise["Valor_Total_Compras"]
            / df_analise["Cliques_Shopee"]
        ),
        np.nan
    )

    # ========================================================
    # 11. GMV POR 1.000 VIEWS
    # ========================================================

    df_analise[
        "GMV_por_1k_Views(R$)"
    ] = np.where(
        df_analise["Visualizacoes"] > 0,
        (
            df_analise["Valor_Total_Compras"]
            / df_analise["Visualizacoes"]
        ) * 1000,
        np.nan
    )

    # ========================================================
    # 12. COMISSÃO POR CLIQUE / RPC
    # ========================================================

    df_analise[
        "Comissao_por_Clique(R$)"
    ] = np.where(
        df_analise["Cliques_Shopee"] > 0,
        (
            df_analise["Comissao_Gerada"]
            / df_analise["Cliques_Shopee"]
        ),
        np.nan
    )

    # ========================================================
    # 13. COMISSÃO POR PEDIDO
    # ========================================================

    df_analise[
        "Comissao_por_Pedido(R$)"
    ] = np.where(
        df_analise["Compras_Shopee"] > 0,
        (
            df_analise["Comissao_Gerada"]
            / df_analise["Compras_Shopee"]
        ),
        np.nan
    )

    # ========================================================
    # 14. COMISSÃO POR 1.000 VIEWS / RPV
    # ========================================================

    df_analise[
        "Comissao_por_1k_Views(R$)"
    ] = np.where(
        df_analise["Visualizacoes"] > 0,
        (
            df_analise["Comissao_Gerada"]
            / df_analise["Visualizacoes"]
        ) * 1000,
        np.nan
    )

    # ========================================================
    # 15. TAXA EFETIVA DE COMISSÃO
    # ========================================================

    df_analise[
        "Taxa_Comissao_Efetiva(%)"
    ] = np.where(
        df_analise["Valor_Total_Compras"] > 0,
        (
            df_analise["Comissao_Gerada"]
            / df_analise["Valor_Total_Compras"]
        ) * 100,
        np.nan
    )

    # ========================================================
    # DIAGNÓSTICO DE ATRIBUIÇÃO
    # ========================================================

    df_analise[
        "Shopee_Atribuicao_Status"
    ] = np.where(
        df_analise[
            "SubID_Unico_No_Post"
        ],
        "ATRIBUÍVEL_AO_POST",
        "SUB_ID_REPETIDO"
    )

    # ========================================================
    # LIMPEZA DE INF / -INF
    # ========================================================

    df_analise = (
        df_analise
        .replace(
            [np.inf, -np.inf],
            np.nan
        )
    )

    # ========================================================
    # ORDENAÇÃO
    # ========================================================

    # Colunas de identificação primeiro
    colunas_identificacao = [
        "Post_ID",
        "sub_id1",
        "sub_id2",
        "Data_Publicacao",
        "Hora_Publicacao",
        "Dia_Semana",
        "Mes",
        "Ano_Mes",
        "Tipo_Conteudo",
        "Media_Type_API",
        "Tipo_Conteudo_API",
        "Canal",
        "Posts_Com_Mesmo_SubID",
        "SubID_Unico_No_Post",
        "Alerta_Atribuicao",
        "Shopee_Atribuicao_Status",
        "Meta_Status",
        "Meta_Erro",
    ]

    colunas_identificacao = [
        coluna
        for coluna in colunas_identificacao
        if coluna in df_analise.columns
    ]

    # Métricas
    colunas_metricas = [
        "Visualizacoes",
        "Curtidas",
        "Comentarios",
        "Salvamentos",
        "Compartilhamentos",
        "Interacoes",
        "Taxa_Engajamento(%)",
        "Curtidas_1k_Views",
        "Salvamentos_1k_Views",
        "Compartilhamentos_1k_Views",
        "Interacoes_1k_Views",

        "Cliques_Shopee",
        "CTR_Shopee(%)",

        "Compras_Shopee",
        "Conversao_Clique_Pedido(%)",
        "Conversao_View_Pedido(%)",
        "Pedidos_1k_Views",

        "Valor_Total_Compras",
        "Ticket_Medio",
        "Ticket_Mediano",
        "Ticket_P25",
        "Ticket_P75",
        "Maior_Pedido",
        "Menor_Pedido",

        "GMV_por_Clique(R$)",
        "GMV_por_1k_Views(R$)",

        "Comissao_Gerada",
        "Comissao_por_Clique(R$)",
        "Comissao_por_Pedido(R$)",
        "Comissao_por_1k_Views(R$)",
        "Taxa_Comissao_Efetiva(%)",
    ]

    colunas_metricas = [
        coluna
        for coluna in colunas_metricas
        if coluna in df_analise.columns
    ]

    colunas_restantes = [
        coluna
        for coluna in df_analise.columns
        if coluna not in (
            colunas_identificacao
            + colunas_metricas
        )
    ]

    df_analise = df_analise[
        colunas_identificacao
        + colunas_metricas
        + colunas_restantes
    ]

    # ========================================================
    # ARREDONDAMENTO
    # ========================================================

    # Quantidades
    colunas_inteiras = [
        "Visualizacoes",
        "Curtidas",
        "Comentarios",
        "Salvamentos",
        "Compartilhamentos",
        "Interacoes",
        "Cliques_Shopee",
        "Compras_Shopee",
    ]

    for coluna in colunas_inteiras:

        if coluna in df_analise.columns:

            df_analise[coluna] = (
                pd.to_numeric(
                    df_analise[coluna],
                    errors="coerce"
                )
            )

    # Financeiros
    colunas_financeiras = [
        "Valor_Total_Compras",
        "Ticket_Medio",
        "Ticket_Mediano",
        "Ticket_P25",
        "Ticket_P75",
        "Maior_Pedido",
        "Menor_Pedido",
        "GMV_por_Clique(R$)",
        "GMV_por_1k_Views(R$)",
        "Comissao_Gerada",
        "Comissao_por_Clique(R$)",
        "Comissao_por_Pedido(R$)",
        "Comissao_por_1k_Views(R$)",
    ]

    for coluna in colunas_financeiras:

        if coluna in df_analise.columns:

            df_analise[coluna] = (
                pd.to_numeric(
                    df_analise[coluna],
                    errors="coerce"
                )
                .round(4)
            )

    # Taxas
    colunas_taxas = [
        "Taxa_Engajamento(%)",
        "CTR_Shopee(%)",
        "Conversao_Clique_Pedido(%)",
        "Conversao_View_Pedido(%)",
        "Taxa_Comissao_Efetiva(%)",
        "Curtidas_1k_Views",
        "Salvamentos_1k_Views",
        "Compartilhamentos_1k_Views",
        "Interacoes_1k_Views",
        "Pedidos_1k_Views",
    ]

    for coluna in colunas_taxas:

        if coluna in df_analise.columns:

            df_analise[coluna] = (
                pd.to_numeric(
                    df_analise[coluna],
                    errors="coerce"
                )
                .round(4)
            )

    # ========================================================
    # DASHBOARD POR SUB_ID
    # ========================================================
    #
    # Esta tabela é muito importante.
    #
    # Se um mesmo sub_id estiver associado a vários posts,
    # NÃO devemos somar a comissão repetidamente para cada
    # post.
    #
    # Aqui os dados Shopee entram apenas uma vez por sub_id.
    #

    print(
        "Criando Dashboard_SubID..."
    )

    # Instagram agregado por subid
    instagram_subid = (
        df_analise
        .groupby(
            [
                "sub_id1",
                "sub_id2"
            ],
            dropna=False
        )
        .agg(

            Posts=(
                "Post_ID",
                "nunique"
            ),

            Visualizacoes=(
                "Visualizacoes",
                "sum"
            ),

            Curtidas=(
                "Curtidas",
                "sum"
            ),

            Comentarios=(
                "Comentarios",
                "sum"
            ),

            Salvamentos=(
                "Salvamentos",
                "sum"
            ),

            Compartilhamentos=(
                "Compartilhamentos",
                "sum"
            )
        )
        .reset_index()
    )

    instagram_subid["Interacoes"] = (
        instagram_subid["Curtidas"].fillna(0)
        + instagram_subid["Comentarios"].fillna(0)
        + instagram_subid["Salvamentos"].fillna(0)
        + instagram_subid["Compartilhamentos"].fillna(0)
    )

    dashboard_subid = pd.merge(
        instagram_subid,
        shopee_consolidado,
        on=[
            "sub_id1",
            "sub_id2"
        ],
        how="outer"
    )

    # --------------------------------------------------------
    # ZEROS SHOPEE
    # --------------------------------------------------------

    dashboard_subid[
        [
            "Cliques_Shopee",
            "Compras_Shopee",
            "Valor_Total_Compras",
            "Comissao_Gerada",
        ]
    ] = (
        dashboard_subid[
            [
                "Cliques_Shopee",
                "Compras_Shopee",
                "Valor_Total_Compras",
                "Comissao_Gerada",
            ]
        ]
        .fillna(0)
    )

    # --------------------------------------------------------
    # KPIs SUB_ID
    # --------------------------------------------------------

    dashboard_subid[
        "Taxa_Engajamento(%)"
    ] = np.where(
        dashboard_subid["Visualizacoes"] > 0,
        (
            dashboard_subid["Interacoes"]
            / dashboard_subid["Visualizacoes"]
        ) * 100,
        np.nan
    )

    dashboard_subid[
        "CTR_Shopee(%)"
    ] = np.where(
        dashboard_subid["Visualizacoes"] > 0,
        (
            dashboard_subid["Cliques_Shopee"]
            / dashboard_subid["Visualizacoes"]
        ) * 100,
        np.nan
    )

    dashboard_subid[
        "Conversao_Clique_Pedido(%)"
    ] = np.where(
        dashboard_subid["Cliques_Shopee"] > 0,
        (
            dashboard_subid["Compras_Shopee"]
            / dashboard_subid["Cliques_Shopee"]
        ) * 100,
        np.nan
    )

    dashboard_subid[
        "Conversao_View_Pedido(%)"
    ] = np.where(
        dashboard_subid["Visualizacoes"] > 0,
        (
            dashboard_subid["Compras_Shopee"]
            / dashboard_subid["Visualizacoes"]
        ) * 100,
        np.nan
    )

    dashboard_subid[
        "GMV_por_1k_Views(R$)"
    ] = np.where(
        dashboard_subid["Visualizacoes"] > 0,
        (
            dashboard_subid["Valor_Total_Compras"]
            / dashboard_subid["Visualizacoes"]
        ) * 1000,
        np.nan
    )

    dashboard_subid[
        "Comissao_por_1k_Views(R$)"
    ] = np.where(
        dashboard_subid["Visualizacoes"] > 0,
        (
            dashboard_subid["Comissao_Gerada"]
            / dashboard_subid["Visualizacoes"]
        ) * 1000,
        np.nan
    )

    dashboard_subid[
        "Comissao_por_Clique(R$)"
    ] = np.where(
        dashboard_subid["Cliques_Shopee"] > 0,
        (
            dashboard_subid["Comissao_Gerada"]
            / dashboard_subid["Cliques_Shopee"]
        ),
        np.nan
    )

    dashboard_subid[
        "Comissao_por_Pedido(R$)"
    ] = np.where(
        dashboard_subid["Compras_Shopee"] > 0,
        (
            dashboard_subid["Comissao_Gerada"]
            / dashboard_subid["Compras_Shopee"]
        ),
        np.nan
    )

    dashboard_subid[
        "Taxa_Comissao_Efetiva(%)"
    ] = np.where(
        dashboard_subid["Valor_Total_Compras"] > 0,
        (
            dashboard_subid["Comissao_Gerada"]
            / dashboard_subid["Valor_Total_Compras"]
        ) * 100,
        np.nan
    )

    dashboard_subid = (
        dashboard_subid
        .replace(
            [np.inf, -np.inf],
            np.nan
        )
    )

    # ========================================================
    # DIAGNÓSTICO
    # ========================================================

    print(
        "Criando Diagnostico..."
    )

    diagnostico = []

    # --------------------------------------------------------
    # Meta
    # --------------------------------------------------------

    if "Meta_Status" in df_analise.columns:

        meta_ok = (
            df_analise["Meta_Status"]
            == "OK"
        ).sum()

        meta_erro = (
            df_analise["Meta_Status"]
            != "OK"
        ).sum()

        diagnostico.append({
            "Tipo": "META",
            "Indicador": "Posts processados",
            "Valor": len(df_analise),
            "Observacao": ""
        })

        diagnostico.append({
            "Tipo": "META",
            "Indicador": "Posts OK",
            "Valor": meta_ok,
            "Observacao": ""
        })

        diagnostico.append({
            "Tipo": "META",
            "Indicador": "Posts com erro",
            "Valor": meta_erro,
            "Observacao": (
                "Verifique Meta_Erro"
                if meta_erro > 0
                else ""
            )
        })

    # --------------------------------------------------------
    # Visualizações
    # --------------------------------------------------------

    if "Visualizacoes" in df_analise.columns:

        views_validas = (
            df_analise["Visualizacoes"]
            .notna()
        ).sum()

        views_zero = (
            df_analise["Visualizacoes"]
            == 0
        ).sum()

        diagnostico.append({
            "Tipo": "INSTAGRAM",
            "Indicador": "Posts com views",
            "Valor": views_validas,
            "Observacao": ""
        })

        diagnostico.append({
            "Tipo": "INSTAGRAM",
            "Indicador": "Posts com views = 0",
            "Valor": views_zero,
            "Observacao": (
                "Pode indicar ausência real "
                "ou indisponibilidade da API"
            )
        })

    # --------------------------------------------------------
    # Sub IDs duplicados
    # --------------------------------------------------------

    duplicados = (
        df_semente[
            df_semente[
                "Posts_Com_Mesmo_SubID"
            ] > 1
        ][
            [
                "sub_id1",
                "sub_id2",
                "Posts_Com_Mesmo_SubID"
            ]
        ]
        .drop_duplicates()
    )

    diagnostico.append({
        "Tipo": "ATRIBUICAO",
        "Indicador": "Combinações sub_id repetidas",
        "Valor": len(duplicados),
        "Observacao": (
            "Cada combinação sub_id1 + sub_id2 "
            "deveria ser única por post se "
            "a intenção for atribuição individual."
        )
    })

    # --------------------------------------------------------
    # Valores financeiros negativos
    # --------------------------------------------------------

    valores_negativos = (
        (
            df_vendas[
                "Valor de Compra(R$)"
            ] < 0
        ).sum()
    )

    comissoes_negativas = (
        (
            df_vendas[
                "Comissão líquida do afiliado(R$)"
            ] < 0
        ).sum()
    )

    diagnostico.append({
        "Tipo": "SHOPEE",
        "Indicador": "Valores de compra negativos",
        "Valor": valores_negativos,
        "Observacao": (
            "Pode representar ajuste/reembolso."
        )
    })

    diagnostico.append({
        "Tipo": "SHOPEE",
        "Indicador": "Comissões negativas",
        "Valor": comissoes_negativas,
        "Observacao": (
            "Pode representar ajuste/reembolso."
        )
    })

    df_diagnostico = pd.DataFrame(
        diagnostico
    )

    # ========================================================
    # EXPORTAÇÃO
    # ========================================================

    print("=" * 70)
    print("ESCREVENDO RESULTADOS")
    print("=" * 70)

    # Dashboard principal
    escrever_aba(
        planilha,
        "Dashboard",
        df_analise
    )

    # Dashboard por subid
    escrever_aba(
        planilha,
        "Dashboard_SubID",
        dashboard_subid
    )

    # Diagnóstico
    escrever_aba(
        planilha,
        "Diagnostico",
        df_diagnostico
    )

    print("=" * 70)
    print("PIPELINE CONCLUÍDO")
    print("=" * 70)

    print(
        f"Posts processados: "
        f"{len(df_analise)}"
    )

    if "Meta_Status" in df_analise.columns:

        print(
            "Posts com Meta OK: "
            f"{(df_analise['Meta_Status'] == 'OK').sum()}"
        )

        print(
            "Posts com erro Meta: "
            f"{(df_analise['Meta_Status'] != 'OK').sum()}"
        )

    print(
        f"Combinações sub_id: "
        f"{len(dashboard_subid)}"
    )

    print(
        "Abas atualizadas:"
    )

    print(
        "  - Dashboard"
    )

    print(
        "  - Dashboard_SubID"
    )

    print(
        "  - Diagnostico"
    )


# ============================================================
# EXECUÇÃO
# ============================================================

if __name__ == "__main__":
    run_pipeline()
