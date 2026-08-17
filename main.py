import pandas as pd
import numpy as np
import requests
import os
import json
import gspread
import numbers
from oauth2client.service_account import ServiceAccountCredentials


ACCESS_TOKEN = os.environ.get('META_ACCESS_TOKEN')
IG_ACCOUNT_ID = os.environ.get('META_IG_ACCOUNT_ID')
GCP_CREDENTIALS = os.environ.get('GCP_CREDENTIALS')
SHEET_ID = os.environ.get('GOOGLE_SHEET_ID')


def limpar_moeda_seguro(valor):
    """
    Converte valores monetários sem destruir decimais válidos.

    Exemplos:
        35.99        -> 35.99
        1.077        -> 1.077
        1,077        -> 1.077
        1.234,56     -> 1234.56
        1,234.56     -> 1234.56
    """

    if pd.isna(valor) or valor == '':
        return 0.0

    # Se já for número, não transforma novamente
    if isinstance(valor, numbers.Number):
        return float(valor)

    val_str = str(valor).strip()

    val_str = (
        val_str
        .replace('R$', '')
        .replace('\xa0', '')
        .strip()
    )

    if not val_str:
        return 0.0

    # Formato brasileiro / internacional com milhar + decimal
    if ',' in val_str and '.' in val_str:

        # Exemplo brasileiro: 1.234,56
        if val_str.rfind(',') > val_str.rfind('.'):
            val_str = val_str.replace('.', '')
            val_str = val_str.replace(',', '.')

        # Exemplo internacional: 1,234.56
        else:
            val_str = val_str.replace(',', '')

    # Apenas vírgula:
    # 123,45 -> 123.45
    elif ',' in val_str:
        val_str = val_str.replace(',', '.')

    # Apenas ponto:
    # NÃO remover o ponto.
    #
    # 1.077 continua sendo 1.077
    # 35.99 continua sendo 35.99
    # 1234.56 continua sendo 1234.56

    try:
        return float(val_str)
    except ValueError:
        print(f'Valor monetário inválido: {valor}')
        return 0.0


def normalizar_subid(serie):
    """
    Padroniza sub_ids para evitar falhas de merge.
    """

    serie = (
        serie
        .fillna('')
        .astype(str)
        .str.strip()
        .str.lower()
    )

    serie = serie.replace({
        '(vazio)': '',
        'nan': '',
        'none': '',
        'null': '',
    })

    return serie


def run_pipeline():

    print("Autenticando no Google Drive/Sheets...")

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    creds_dict = json.loads(GCP_CREDENTIALS)

    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict,
        scope
    )

    client = gspread.authorize(creds)

    planilha = client.open_by_key(SHEET_ID)

    print("Lendo abas...")

    df_semente = pd.DataFrame(
        planilha.worksheet("Tabela_Semente").get_all_records()
    )

    df_cliques = pd.DataFrame(
        planilha.worksheet("Shopee_Cliques").get_all_records()
    )

    df_vendas = pd.DataFrame(
    planilha.worksheet("Shopee_Vendas").get_all_records(
        value_render_option='UNFORMATTED_VALUE'
        )
    )

    if df_semente.empty:
        print("Tabela Semente vazia. Encerrando.")
        return

    # =========================================================
    # 1. PADRONIZAÇÃO DA SEMENTE
    # =========================================================

    df_semente['Post_ID'] = (
        df_semente['Post_ID']
        .astype(str)
        .str.strip()
    )

    df_semente['sub_id1'] = normalizar_subid(
        df_semente['sub_id1']
    )

    df_semente['sub_id2'] = normalizar_subid(
        df_semente['sub_id2']
    )

    # =========================================================
    # 2. META / INSTAGRAM
    # =========================================================

    print("Extraindo dados da Meta API...")

    dados_ig = []

    for post_id in df_semente['Post_ID']:

        if not post_id or post_id == 'nan':
            continue

        url = (
            f"https://graph.facebook.com/v18.0/{post_id}"
            f"?fields=insights.metric("
            f"impressions,likes,comments,saved,shares"
            f")&access_token={ACCESS_TOKEN}"
        )

        try:

            response = requests.get(url, timeout=30).json()

            insights = (
                response
                .get('insights', {})
                .get('data', [])
            )

            metrics = {
                'impressions': 0,
                'likes': 0,
                'comments': 0,
                'saved': 0,
                'shares': 0
            }

            for insight in insights:

                nome_metrica = insight.get('name')

                if nome_metrica in metrics:

                    metrics[nome_metrica] = (
                        insight['values'][0]['value']
                    )

            dados_ig.append({
                'Post_ID': post_id,
                'Visualizacoes': metrics['impressions'],
                'Curtidas': metrics['likes'],
                'Comentarios': metrics['comments'],
                'Salvamentos': metrics['saved'],
                'Compartilhamentos': metrics['shares']
            })

        except Exception as e:

            print(f"Erro no Post {post_id}: {e}")

            dados_ig.append({
                'Post_ID': post_id,
                'Visualizacoes': 0,
                'Curtidas': 0,
                'Comentarios': 0,
                'Salvamentos': 0,
                'Compartilhamentos': 0
            })

    df_meta = pd.DataFrame(dados_ig)

    df_analise = pd.merge(
        df_semente,
        df_meta,
        on='Post_ID',
        how='left'
    )

    # =========================================================
    # 3. CLIQUES SHOPEE
    # =========================================================

    print("Processando Cliques da Shopee...")

    df_cliques['Sub_id'] = (
        df_cliques['Sub_id']
        .fillna('')
        .astype(str)
        .str.strip()
    )

    def extrair_subs_clique(sub_str):

        partes = [
            p.strip().lower()
            for p in sub_str.split('-')
            if p.strip()
        ]

        sub1 = partes[0] if len(partes) > 0 else ''
        sub2 = partes[1] if len(partes) > 1 else ''

        return pd.Series([sub1, sub2])

    df_cliques[['sub_id1', 'sub_id2']] = (
        df_cliques['Sub_id']
        .apply(extrair_subs_clique)
    )

    shopee_cliques_agrupado = (
        df_cliques
        .groupby(
            ['sub_id1', 'sub_id2'],
            dropna=False
        )
        .size()
        .reset_index(name='Cliques_Shopee')
    )

    # =========================================================
    # 4. VENDAS SHOPEE
    # =========================================================

    print("Processando Vendas da Shopee...")

    df_vendas['sub_id1'] = normalizar_subid(
        df_vendas['Sub_id1']
    )

    df_vendas['sub_id2'] = normalizar_subid(
        df_vendas['Sub_id2']
    )

    # =========================================================
    # 5. CAMPOS FINANCEIROS
    # =========================================================

    df_vendas['Valor de Compra(R$)'] = pd.to_numeric(
        df_vendas['Valor de Compra(R$)'],
        errors='coerce'
    ).fillna(0)
    
    df_vendas['Comissão líquida do afiliado(R$)'] = pd.to_numeric(
        df_vendas['Comissão líquida do afiliado(R$)'],
        errors='coerce'
    ).fillna(0)

    # =========================================================
    # 6. VENDAS AGREGADAS
    # =========================================================

    shopee_vendas_agrupado = (
        df_vendas
        .groupby(
            ['sub_id1', 'sub_id2'],
            dropna=False
        )
        .agg(

            # IMPORTANTE:
            # conta pedidos únicos, não linhas
            Compras_Shopee=(
                'ID do pedido',
                'nunique'
            ),

            # Soma dos itens
            Valor_Total_Compras=(
                'Valor de Compra(R$)',
                'sum'
            ),

            # Soma das comissões dos itens
            Comissao_Gerada=(
                'Comissão líquida do afiliado(R$)',
                'sum'
            )
        )
        .reset_index()
    )

    # =========================================================
    # 7. TICKET MÉDIO POR PEDIDO
    # =========================================================

    # Primeiro soma todos os itens pertencentes ao mesmo pedido
    valor_por_pedido = (
        df_vendas
        .groupby(
            ['sub_id1', 'sub_id2', 'ID do pedido'],
            dropna=False
        )['Valor de Compra(R$)']
        .sum()
        .reset_index()
    )

    # Depois calcula média dos pedidos
    ticket_medio = (
        valor_por_pedido
        .groupby(
            ['sub_id1', 'sub_id2'],
            dropna=False
        )['Valor de Compra(R$)']
        .mean()
        .reset_index()
    )

    ticket_medio = ticket_medio.rename(
        columns={
            'Valor de Compra(R$)': 'Ticket_Medio(R$)'
        }
    )

    # =========================================================
    # 8. CONSOLIDAÇÃO SHOPEE
    # =========================================================

    shopee_consolidado = pd.merge(
        shopee_cliques_agrupado,
        shopee_vendas_agrupado,
        on=['sub_id1', 'sub_id2'],
        how='outer'
    )

    shopee_consolidado = pd.merge(
        shopee_consolidado,
        ticket_medio,
        on=['sub_id1', 'sub_id2'],
        how='outer'
    )

    # =========================================================
    # 9. MERGE COM SEMENTE / META
    # =========================================================

    df_analise = pd.merge(
        df_analise,
        shopee_consolidado,
        on=['sub_id1', 'sub_id2'],
        how='left'
    )

    # Somente colunas numéricas recebem zero
    colunas_numericas = [
        'Visualizacoes',
        'Curtidas',
        'Comentarios',
        'Salvamentos',
        'Compartilhamentos',
        'Cliques_Shopee',
        'Compras_Shopee',
        'Valor_Total_Compras',
        'Comissao_Gerada',
        'Ticket_Medio(R$)'
    ]

    df_analise[colunas_numericas] = (
        df_analise[colunas_numericas]
        .fillna(0)
    )

    # =========================================================
    # 10. KPIs
    # =========================================================

    interacoes = (
        df_analise['Curtidas']
        + df_analise['Comentarios']
        + df_analise['Salvamentos']
        + df_analise['Compartilhamentos']
    )

    df_analise['Taxa_Engajamento(%)'] = np.where(
        df_analise['Visualizacoes'] > 0,
        (
            interacoes
            / df_analise['Visualizacoes']
        ) * 100,
        0
    )

    df_analise['Taxa_Conversao(%)'] = np.where(
        df_analise['Cliques_Shopee'] > 0,
        (
            df_analise['Compras_Shopee']
            / df_analise['Cliques_Shopee']
        ) * 100,
        0
    )

    df_analise['RPC_por_clique(R$)'] = np.where(
        df_analise['Cliques_Shopee'] > 0,
        (
            df_analise['Comissao_Gerada']
            / df_analise['Cliques_Shopee']
        ),
        0
    )

    df_analise['RPV_1k_views(R$)'] = np.where(
        df_analise['Visualizacoes'] > 0,
        (
            df_analise['Comissao_Gerada']
            / df_analise['Visualizacoes']
        ) * 1000,
        0
    )

    # =========================================================
    # 11. LIMPEZA FINAL
    # =========================================================

    df_analise = (
        df_analise
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0)
    )

    # Arredonda somente métricas numéricas
    df_analise[colunas_numericas] = (
        df_analise[colunas_numericas]
        .round(2)
    )

    df_analise['Taxa_Engajamento(%)'] = (
        df_analise['Taxa_Engajamento(%)'].round(2)
    )

    df_analise['Taxa_Conversao(%)'] = (
        df_analise['Taxa_Conversao(%)'].round(2)
    )

    df_analise['RPC_por_clique(R$)'] = (
        df_analise['RPC_por_clique(R$)'].round(4)
    )

    df_analise['RPV_1k_views(R$)'] = (
        df_analise['RPV_1k_views(R$)'].round(4)
    )

    # =========================================================
    # 12. GOOGLE SHEETS
    # =========================================================

    print("Enviando resultados para Dashboard...")

    aba_dashboard = planilha.worksheet("Dashboard")

    aba_dashboard.clear()

    dados_para_enviar = (
        [df_analise.columns.tolist()]
        + df_analise.values.tolist()
    )

    aba_dashboard.update(dados_para_enviar)

    print("Pipeline concluído com sucesso.")

if __name__ == "__main__":
    run_pipeline()
