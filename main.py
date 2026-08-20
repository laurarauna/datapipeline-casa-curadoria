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
    """
    if pd.isna(valor) or valor == '':
        return 0.0

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

    if ',' in val_str and '.' in val_str:
        if val_str.rfind(',') > val_str.rfind('.'):
            val_str = val_str.replace('.', '')
            val_str = val_str.replace(',', '.')
        else:
            val_str = val_str.replace(',', '')
    elif ',' in val_str:
        val_str = val_str.replace(',', '.')

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

    df_semente = pd.DataFrame(planilha.worksheet("Tabela_Semente").get_all_records())
    df_cliques = pd.DataFrame(planilha.worksheet("Shopee_Cliques").get_all_records())
    df_vendas = pd.DataFrame(planilha.worksheet("Shopee_Vendas").get_all_records(value_render_option='UNFORMATTED_VALUE'))

    if df_semente.empty:
        print("Tabela Semente vazia. Encerrando.")
        return

    # =========================================================
    # 1. PADRONIZAÇÃO DA SEMENTE
    # =========================================================

    df_semente['Post_ID'] = df_semente['Post_ID'].astype(str).str.strip()
    df_semente['sub_id1'] = normalizar_subid(df_semente['sub_id1'])
    df_semente['sub_id2'] = normalizar_subid(df_semente['sub_id2'])

    # =========================================================
    # 2. META / INSTAGRAM (Agora puxando o Timestamp)
    # =========================================================

    print("Extraindo dados da Meta API...")
    dados_ig = []

    for post_id in df_semente['Post_ID']:
        if not post_id or post_id == 'nan':
            continue

        # Adicionado o campo 'timestamp' na URL
        url = (
            f"https://graph.facebook.com/v18.0/{post_id}"
            f"?fields=timestamp,insights.metric("
            f"views,likes,comments,saved,shares"
            f")&access_token={ACCESS_TOKEN}"
        )

        try:
            response = requests.get(url, timeout=30).json()
            insights = response.get('insights', {}).get('data', [])
            
            # Captura a data de criação do post
            data_postagem = response.get('timestamp', '')

            metrics = {'views': 0, 'likes': 0, 'comments': 0, 'saved': 0, 'shares': 0}

            for insight in insights:
                nome_metrica = insight.get('name')
                if nome_metrica in metrics:
                    metrics[nome_metrica] = insight['values'][0]['value']

            dados_ig.append({
                'Post_ID': post_id,
                'Data_Postagem': data_postagem,
                'Visualizacoes': metrics['views'],
                'Curtidas': metrics['likes'],
                'Comentarios': metrics['comments'],
                'Salvamentos': metrics['saved'],
                'Compartilhamentos': metrics['shares']
            })

        except Exception as e:
            print(f"Erro no Post {post_id}: {e}")
            dados_ig.append({
                'Post_ID': post_id, 'Data_Postagem': '', 'Visualizacoes': 0, 
                'Curtidas': 0, 'Comentarios': 0, 'Salvamentos': 0, 'Compartilhamentos': 0
            })

    df_meta = pd.DataFrame(dados_ig)
    
    # Formata a Data_Postagem para o padrão legível (YYYY-MM-DD HH:MM:SS)
    df_meta['Data_Postagem'] = pd.to_datetime(df_meta['Data_Postagem'], errors='coerce').dt.strftime('%Y-%m-%d %H:%M:%S').fillna('')

    df_analise = pd.merge(df_semente, df_meta, on='Post_ID', how='left')

    # =========================================================
    # 3. CLIQUES SHOPEE (Adicionado Data do Último Clique)
    # =========================================================

    print("Processando Cliques da Shopee...")
    df_cliques['Sub_id'] = df_cliques['Sub_id'].fillna('').astype(str).str.strip()
    
    # Converte a coluna para data para extrair o máximo
    df_cliques['Tempo dos Cliques'] = pd.to_datetime(df_cliques['Tempo dos Cliques'], errors='coerce')

    def extrair_subs_clique(sub_str):
        partes = [p.strip().lower() for p in sub_str.split('-') if p.strip()]
        sub1 = partes[0] if len(partes) > 0 else ''
        sub2 = partes[1] if len(partes) > 1 else ''
        return pd.Series([sub1, sub2])

    df_cliques[['sub_id1', 'sub_id2']] = df_cliques['Sub_id'].apply(extrair_subs_clique)

    shopee_cliques_agrupado = (
        df_cliques
        .groupby(['sub_id1', 'sub_id2'], dropna=False)
        .agg(
            Cliques_Shopee=('Sub_id', 'size'),
            Data_Ultimo_Clique=('Tempo dos Cliques', 'max') # Nova Coluna
        )
        .reset_index()
    )
    # Formata de volta para string para subir pro Sheets sem erro
    shopee_cliques_agrupado['Data_Ultimo_Clique'] = shopee_cliques_agrupado['Data_Ultimo_Clique'].dt.strftime('%Y-%m-%d %H:%M:%S').fillna('')

    # =========================================================
    # 4. VENDAS SHOPEE (Adicionado Data da Última Venda)
    # =========================================================

    print("Processando Vendas da Shopee...")
    df_vendas['sub_id1'] = normalizar_subid(df_vendas['Sub_id1'])
    df_vendas['sub_id2'] = normalizar_subid(df_vendas['Sub_id2'])

    # Tratamento da Data do Pedido
    df_vendas['Horário do pedido'] = pd.to_datetime(df_vendas['Horário do pedido'], errors='coerce')

    # =========================================================
    # 5. CAMPOS FINANCEIROS
    # =========================================================

    df_vendas['Valor de Compra(R$)'] = pd.to_numeric(df_vendas['Valor de Compra(R$)'].apply(limpar_moeda_seguro), errors='coerce').fillna(0)
    df_vendas['Comissão líquida do afiliado(R$)'] = pd.to_numeric(df_vendas['Comissão líquida do afiliado(R$)'].apply(limpar_moeda_seguro), errors='coerce').fillna(0)

    # =========================================================
    # 6. VENDAS AGREGADAS
    # =========================================================

    shopee_vendas_agrupado = (
        df_vendas
        .groupby(['sub_id1', 'sub_id2'], dropna=False)
        .agg(
            Compras_Shopee=('ID do pedido', 'nunique'),
            Valor_Total_Compras=('Valor de Compra(R$)', 'sum'),
            Comissao_Gerada=('Comissão líquida do afiliado(R$)', 'sum'),
            Data_Ultima_Venda=('Horário do pedido', 'max') # Nova Coluna
        )
        .reset_index()
    )
    # Formata de volta para string
    shopee_vendas_agrupado['Data_Ultima_Venda'] = shopee_vendas_agrupado['Data_Ultima_Venda'].dt.strftime('%Y-%m-%d %H:%M:%S').fillna('')

    # =========================================================
    # 7. TICKET MÉDIO POR PEDIDO
    # =========================================================

    valor_por_pedido = (
        df_vendas
        .groupby(['sub_id1', 'sub_id2', 'ID do pedido'], dropna=False)['Valor de Compra(R$)']
        .sum()
        .reset_index()
    )

    ticket_medio = (
        valor_por_pedido
        .groupby(['sub_id1', 'sub_id2'], dropna=False)['Valor de Compra(R$)']
        .mean()
        .reset_index()
    )

    ticket_medio = ticket_medio.rename(columns={'Valor de Compra(R$)': 'Ticket_Medio(R$)'})

    # =========================================================
    # 8. CONSOLIDAÇÃO SHOPEE
    # =========================================================

    shopee_consolidado = pd.merge(shopee_cliques_agrupado, shopee_vendas_agrupado, on=['sub_id1', 'sub_id2'], how='outer')
    shopee_consolidado = pd.merge(shopee_consolidado, ticket_medio, on=['sub_id1', 'sub_id2'], how='outer')

    # =========================================================
    # 9. MERGE COM SEMENTE / META
    # =========================================================

    df_analise = pd.merge(df_analise, shopee_consolidado, on=['sub_id1', 'sub_id2'], how='left')

    colunas_numericas = [
        'Visualizacoes', 'Curtidas', 'Comentarios', 'Salvamentos', 'Compartilhamentos',
        'Cliques_Shopee', 'Compras_Shopee', 'Valor_Total_Compras', 'Comissao_Gerada', 'Ticket_Medio(R$)'
    ]
    df_analise[colunas_numericas] = df_analise[colunas_numericas].fillna(0)
    
    colunas_texto = ['Data_Postagem', 'Data_Ultimo_Clique', 'Data_Ultima_Venda']
    for col in colunas_texto:
        if col in df_analise.columns:
            df_analise[col] = df_analise[col].fillna('')

    # =========================================================
    # 10. KPIs
    # =========================================================

    interacoes = (df_analise['Curtidas'] + df_analise['Comentarios'] + df_analise['Salvamentos'] + df_analise['Compartilhamentos'])

    df_analise['Taxa_Engajamento(%)'] = np.where(df_analise['Visualizacoes'] > 0, (interacoes / df_analise['Visualizacoes']) * 100, 0)
    df_analise['Taxa_Conversao(%)'] = np.where(df_analise['Cliques_Shopee'] > 0, (df_analise['Compras_Shopee'] / df_analise['Cliques_Shopee']) * 100, 0)
    df_analise['RPC_por_clique(R$)'] = np.where(df_analise['Cliques_Shopee'] > 0, (df_analise['Comissao_Gerada'] / df_analise['Cliques_Shopee']), 0)
    df_analise['RPV_1k_views(R$)'] = np.where(df_analise['Visualizacoes'] > 0, (df_analise['Comissao_Gerada'] / df_analise['Visualizacoes']) * 1000, 0)

    # =========================================================
    # 11. LIMPEZA FINAL
    # =========================================================

    df_analise = df_analise.replace([np.inf, -np.inf], np.nan)
    df_analise[colunas_numericas] = df_analise[colunas_numericas].fillna(0).round(2)
    
    # KPIs Rounding
    df_analise['Taxa_Engajamento(%)'] = df_analise['Taxa_Engajamento(%)'].fillna(0).round(2)
    df_analise['Taxa_Conversao(%)'] = df_analise['Taxa_Conversao(%)'].fillna(0).round(2)
    df_analise['RPC_por_clique(R$)'] = df_analise['RPC_por_clique(R$)'].fillna(0).round(4)
    df_analise['RPV_1k_views(R$)'] = df_analise['RPV_1k_views(R$)'].fillna(0).round(4)

    # Preenchendo DataFrames para evitar envio de 'NaN' ao Google Sheets
    df_analise = df_analise.fillna('')

    # =========================================================
    # 12. GOOGLE SHEETS
    # =========================================================

    print("Enviando resultados para Dashboard...")
    aba_dashboard = planilha.worksheet("Dashboard")
    aba_dashboard.clear()

    dados_para_enviar = [df_analise.columns.tolist()] + df_analise.values.tolist()
    aba_dashboard.update(dados_para_enviar)

    print("Pipeline concluído com sucesso.")

if __name__ == "__main__":
    run_pipeline()
