import pandas as pd
import numpy as np
import requests
import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Puxa os Secrets configurados no GitHub
ACCESS_TOKEN = os.environ.get('META_ACCESS_TOKEN')
IG_ACCOUNT_ID = os.environ.get('META_IG_ACCOUNT_ID')
GCP_CREDENTIALS = os.environ.get('GCP_CREDENTIALS')
SHEET_ID = os.environ.get('GOOGLE_SHEET_ID')

def limpar_moeda_seguro(valor):
    if pd.isna(valor) or valor == '':
        return 0.0
    if isinstance(valor, (int, float)):
        return float(valor)
    
    val_str = str(valor).replace('R$', '').strip()
    if not val_str:
        return 0.0
    
    # Tratamento para formatos mistos ou padrão brasileiro (vírgula como decimal)
    if ',' in val_str and '.' in val_str:
        if val_str.rfind(',') > val_str.rfind('.'):
            val_str = val_str.replace('.', '').replace(',', '.')
        else:
            val_str = val_str.replace(',', '')
    elif ',' in val_str:
        val_str = val_str.replace(',', '.')
    elif '.' in val_str:
        # Evita remover ponto se for separador decimal legítimo (ex: 35.99 ou 1.0797)
        parts = val_str.split('.')
        if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) <= 3):
            val_str = val_str.replace('.', '')
            
    return float(val_str)

def run_pipeline():
    print("Autenticando no Google Drive/Sheets...")
    
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_dict = json.loads(GCP_CREDENTIALS)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    
    planilha = client.open_by_key(SHEET_ID)
    
    print("Lendo abas de dados do Google Sheets...")
    df_semente = pd.DataFrame(planilha.worksheet("Tabela_Semente").get_all_records())
    df_cliques = pd.DataFrame(planilha.worksheet("Shopee_Cliques").get_all_records())
    df_vendas = pd.DataFrame(planilha.worksheet("Shopee_Vendas").get_all_records())

    if df_semente.empty:
        print("Tabela Semente vazia. Encerrando.")
        return

    # Padronização da Semente
    df_semente['Post_ID'] = df_semente['Post_ID'].astype(str).str.strip()
    df_semente['sub_id1'] = df_semente['sub_id1'].astype(str).str.lower().str.strip()
    df_semente['sub_id2'] = df_semente['sub_id2'].astype(str).str.lower().str.strip()
    df_semente['sub_id2'] = df_semente['sub_id2'].replace(['(vazio)', 'nan', 'none', ''], '')

    # 1. EXTRAÇÃO META API (INSTAGRAM)
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

    # 2. TRANSFORMAÇÃO SHOPEE (CLIQUES)
    print("Processando Cliques da Shopee...")
    df_cliques['Sub_id'] = df_cliques['Sub_id'].astype(str).fillna('')
    
    def extrair_subs_clique(sub_str):
        partes = [p for p in sub_str.split('-') if p]
        sub1 = partes[0].lower().strip() if len(partes) > 0 else ""
        sub2 = partes[1].lower().strip() if len(partes) > 1 else ""
        return pd.Series([sub1, sub2])
        
    df_cliques[['sub_id1', 'sub_id2']] = df_cliques['Sub_id'].apply(extrair_subs_clique)
    shopee_cliques_agrupado = df_cliques.groupby(['sub_id1', 'sub_id2']).size().reset_index(name='Cliques_Shopee')

    # 3. TRANSFORMAÇÃO SHOPEE (VENDAS COM TRATAMENTO SEGURO DE MOEDA)
    print("Processando Vendas da Shopee...")
    df_vendas['sub_id1'] = df_vendas['Sub_id1'].fillna('').astype(str).str.lower().str.strip()
    df_vendas['sub_id2'] = df_vendas['Sub_id2'].fillna('').astype(str).str.lower().str.strip()
    df_vendas['sub_id2'] = df_vendas['sub_id2'].replace(['nan', 'none', ''], '')

    # Aplicação da limpeza segura de decimais nos campos financeiros
    df_vendas['Valor de Compra(R$)'] = df_vendas['Valor de Compra(R$)'].apply(limpar_moeda_seguro)
    df_vendas['Comissão líquida do afiliado(R$)'] = df_vendas['Comissão líquida do afiliado(R$)'].apply(limpar_moeda_seguro)

    # Agrupamento de Vendas e Comissões
    shopee_vendas_agrupado = df_vendas.groupby(['sub_id1', 'sub_id2']).agg(
        Compras_Shopee=('ID do pedido', 'count'),
        Valor_Total_Compras=('Valor de Compra(R$)', 'sum'),
        Comissao_Gerada=('Comissão líquida do afiliado(R$)', 'sum')
    ).reset_index()

    # Cálculo Refinado de Ticket Médio por ID de Pedido
    df_vendas_por_pedido = df_vendas.groupby(['sub_id1', 'sub_id2', 'ID do pedido'])['Valor de Compra(R$)'].sum().reset_index()
    ticket_medio_por_sub = df_vendas_por_pedido.groupby(['sub_id1', 'sub_id2'])['Valor de Compra(R$)'].mean().reset_index()
    ticket_medio_por_sub.columns = ['sub_id1', 'sub_id2', 'Ticket_Medio(R$)']

    # 4. CONSOLIDAÇÃO E MERGE
    print("Consolidando métricas e KPIs...")
    shopee_consolidado = pd.merge(shopee_vendas_agrupado, ticket_medio_por_sub, on=['sub_id1', 'sub_id2'], how='left')
    shopee_consolidado = pd.merge(shopee_cliques_agrupado, shopee_consolidado, on=['sub_id1', 'sub_id2'], how='outer').fillna(0)
    
    df_analise = pd.merge(df_analise, shopee_consolidado, on=['sub_id1', 'sub_id2'], how='left').fillna(0)
    
    # 5. CÁLCULO DOS INDICADORES FINAIS
    interacoes = df_analise['Curtidas'] + df_analise['Comentarios'] + df_analise['Salvamentos'] + df_analise['Compartilhamentos']
    df_analise['Taxa_Engajamento(%)'] = (interacoes / df_analise['Visualizacoes'].replace(0, np.nan)) * 100
    df_analise['Taxa_Conversao(%)'] = (df_analise['Compras_Shopee'] / df_analise['Cliques_Shopee'].replace(0, np.nan)) * 100
    df_analise['RPC_por_clique(R$)'] = df_analise['Comissao_Gerada'] / df_analise['Cliques_Shopee'].replace(0, np.nan)
    df_analise['RPV_1k_views(R$)'] = (df_analise['Comissao_Gerada'] / df_analise['Visualizacoes'].replace(0, np.nan)) * 1000

    df_analise = df_analise.replace([np.inf, -np.inf], np.nan).fillna(0).round(2)

    # 6. ESCREVER NO GOOGLE SHEETS
    print("Enviando resultados para a aba Dashboard...")
    aba_dashboard = planilha.worksheet("Dashboard")
    aba_dashboard.clear()
    dados_para_enviar = [df_analise.columns.values.tolist()] + df_analise.values.tolist()
    aba_dashboard.update(dados_para_enviar)
    
    print("Pipeline concluído com sucesso e valores corrigidos!")

if __name__ == "__main__":
    run_pipeline()
