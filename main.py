import pandas as pd
import numpy as np
import requests
import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# 1. Carrega os Secrets do GitHub
ACCESS_TOKEN = os.environ.get('META_ACCESS_TOKEN')
IG_ACCOUNT_ID = os.environ.get('META_IG_ACCOUNT_ID')
GCP_CREDENTIALS = os.environ.get('GCP_CREDENTIALS')
SHEET_ID = os.environ.get('GOOGLE_SHEET_ID')

def limpar_moeda(valor):
    if pd.isna(valor) or valor == '': return 0.0
    if isinstance(valor, str):
        return float(valor.replace('R$', '').replace('.', '').replace(',', '.').strip())
    return float(valor)

def run_pipeline():
    print("Autenticando no Google Drive/Sheets...")
    
    # 2. Conexão com o Google Sheets
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_dict = json.loads(GCP_CREDENTIALS)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    
    planilha = client.open_by_key(SHEET_ID)
    
    print("Lendo abas de dados...")
    # Lê as abas do Google Sheets e converte para DataFrames
    df_semente = pd.DataFrame(planilha.worksheet("Tabela_Semente").get_all_records())
    df_cliques = pd.DataFrame(planilha.worksheet("Shopee_Cliques").get_all_records())
    df_vendas = pd.DataFrame(planilha.worksheet("Shopee_Vendas").get_all_records())

    if df_semente.empty:
        print("Tabela Semente vazia. Encerrando.")
        return

    # Padroniza Semente
    df_semente['Post_ID'] = df_semente['Post_ID'].astype(str).str.strip()
    df_semente['sub_id1'] = df_semente['sub_id1'].astype(str).str.lower().str.strip()
    df_semente['sub_id2'] = df_semente['sub_id2'].astype(str).str.lower().str.strip()

    # 3. EXTRAÇÃO META API (IG)
    print("Extraindo dados da Meta API...")
    dados_ig = []
    
    for post_id in df_semente['Post_ID']:
        if not post_id or post_id == 'nan': continue
        url = f"https://graph.facebook.com/v18.0/{post_id}?fields=insights.metric(impressions,likes,comments,saved,shares)&access_token={ACCESS_TOKEN}"
        
        try:
            response = requests.get(url).json()
            insights = response.get('insights', {}).get('data', [])
            
            metrics = {'impressions': 0, 'likes': 0, 'comments': 0, 'saved': 0, 'shares': 0}
            for insight in insights:
                nome_metrica = insight.get('name')
                if nome_metrica in metrics:
                    metrics[nome_metrica] = insight['values'][0]['value']
            
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
            dados_ig.append({'Post_ID': post_id, 'Visualizacoes': 0, 'Curtidas': 0, 'Comentarios': 0, 'Salvamentos': 0, 'Compartilhamentos': 0})

    df_meta = pd.DataFrame(dados_ig)
    df_analise = pd.merge(df_semente, df_meta, on='Post_ID', how='left')

    # 4. TRANSFORMAÇÃO SHOPEE (CLIQUES E VENDAS)
    print("Processando Shopee...")
    df_cliques['Sub_id'] = df_cliques['Sub_id'].astype(str).fillna('')
    
    def extrair_subs_clique(sub_str):
        partes = [p for p in sub_str.split('-') if p]
        sub1 = partes[0].lower().strip() if len(partes) > 0 else ""
        sub2 = partes[1].lower().strip() if len(partes) > 1 else ""
        return pd.Series([sub1, sub2])
        
    df_cliques[['sub_id1', 'sub_id2']] = df_cliques['Sub_id'].apply(extrair_subs_clique)
    shopee_cliques_agrupado = df_cliques.groupby(['sub_id1', 'sub_id2']).size().reset_index(name='Cliques_Shopee')

    df_vendas['sub_id1'] = df_vendas['Sub_id1'].astype(str).str.lower().str.strip()
    df_vendas['sub_id2'] = df_vendas['Sub_id2'].astype(str).str.lower().str.strip()
    df_vendas['Valor de Compra(R$)'] = df_vendas['Valor de Compra(R$)'].apply(limpar_moeda)
    df_vendas['Comissão líquida do afiliado(R$)'] = df_vendas['Comissão líquida do afiliado(R$)'].apply(limpar_moeda)

    shopee_vendas_agrupado = df_vendas.groupby(['sub_id1', 'sub_id2']).agg(
        Compras_Shopee=('ID do pedido', 'count'),
        Valor_Total_Compras=('Valor de Compra(R$)', 'sum'),
        Comissao_Gerada=('Comissão líquida do afiliado(R$)', 'sum')
    ).reset_index()

    # 5. MERGE E CÁLCULOS
    print("Consolidando métricas...")
    shopee_consolidado = pd.merge(shopee_cliques_agrupado, shopee_vendas_agrupado, on=['sub_id1', 'sub_id2'], how='outer').fillna(0)
    df_analise = pd.merge(df_analise, shopee_consolidado, on=['sub_id1', 'sub_id2'], how='left').fillna(0)
    
    interacoes = df_analise['Curtidas'] + df_analise['Comentarios'] + df_analise['Salvamentos'] + df_analise['Compartilhamentos']
    df_analise['Taxa_Engajamento(%)'] = (interacoes / df_analise['Visualizacoes'].replace(0, np.nan)) * 100
    df_analise['Taxa_Conversao(%)'] = (df_analise['Compras_Shopee'] / df_analise['Cliques_Shopee'].replace(0, np.nan)) * 100
    df_analise['RPC_por_clique(R$)'] = df_analise['Comissao_Gerada'] / df_analise['Cliques_Shopee'].replace(0, np.nan)
    df_analise['RPV_1k_views(R$)'] = (df_analise['Comissao_Gerada'] / df_analise['Visualizacoes'].replace(0, np.nan)) * 1000
    df_analise['Ticket_Medio(R$)'] = df_analise['Valor_Total_Compras'] / df_analise['Compras_Shopee'].replace(0, np.nan)

    # Prepara o Dataframe para o Google Sheets (Gspread não aceita Infinity ou NaN)
    df_analise = df_analise.replace([np.inf, -np.inf], np.nan).fillna('')
    df_analise = df_analise.round(2)

    # 6. ESCREVER RESULTADO NO GOOGLE SHEETS
    print("Enviando resultados para a aba Dashboard no Google Drive...")
    aba_dashboard = planilha.worksheet("Dashboard")
    aba_dashboard.clear()
    aba_dashboard.update([df_analise.columns.values.tolist()] + df_analise.values.tolist())
    
    print("Pipeline concluído e planilha do Drive atualizada!")

if __name__ == "__main__":
    run_pipeline()
