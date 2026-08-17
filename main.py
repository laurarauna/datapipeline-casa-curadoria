import pandas as pd
import numpy as np
import requests
import os

# Puxa os Secrets configurados no GitHub
ACCESS_TOKEN = os.environ.get('META_ACCESS_TOKEN')
IG_ACCOUNT_ID = os.environ.get('META_IG_ACCOUNT_ID')

# Função para limpar a formatação de moeda (R$ 1.500,00 -> 1500.00)
def limpar_moeda(valor):
    if pd.isna(valor):
        return 0.0
    if isinstance(valor, str):
        return float(valor.replace('R$', '').replace('.', '').replace(',', '.').strip())
    return float(valor)

def run_pipeline():
    print("Iniciando o Pipeline ETL...")
    
    # =======================================================
    # 1. LER DADOS LOCAIS (Pasta data/)
    # =======================================================
    try:
        df_semente = pd.read_csv('data/tabela_semente.csv')
        df_cliques = pd.read_csv('data/shopee_cliques.csv')
        df_vendas = pd.read_csv('data/shopee_vendas.csv')
        print("Arquivos locais carregados com sucesso.")
    except Exception as e:
        print(f"Erro Crítico ao ler arquivos locais na pasta data/: {e}")
        return

    # Padroniza a Semente (Tudo em texto minúsculo e sem espaços em branco)
    df_semente['Post_ID'] = df_semente['Post_ID'].astype(str).str.strip()
    df_semente['sub_id1'] = df_semente['sub_id1'].astype(str).str.lower().str.strip()
    df_semente['sub_id2'] = df_semente['sub_id2'].astype(str).str.lower().str.strip()

    # =======================================================
    # 2. EXTRAÇÃO META API (INSTAGRAM)
    # =======================================================
    dados_ig = []
    print("Extraindo dados da Meta API...")
    
    for post_id in df_semente['Post_ID']:
        url = f"https://graph.facebook.com/v18.0/{post_id}?fields=insights.metric(impressions,likes,comments,saved,shares)&access_token={ACCESS_TOKEN}"
        
        try:
            response = requests.get(url).json()
            insights = response.get('insights', {}).get('data', [])
            
            # Cria um dicionário seguro para buscar as métricas pelo nome 
            # (Evita erro caso a Meta mude a ordem do JSON)
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
            print(f"Post {post_id} extraído com sucesso.")
            
        except Exception as e:
            print(f"Aviso: Não foi possível processar o Post {post_id}. Erro: {e}")
            dados_ig.append({
                'Post_ID': post_id, 'Visualizacoes': 0, 'Curtidas': 0, 
                'Comentarios': 0, 'Salvamentos': 0, 'Compartilhamentos': 0
            })

    # Junta as métricas do IG com a tabela semente
    df_meta = pd.DataFrame(dados_ig)
    df_analise = pd.merge(df_semente, df_meta, on='Post_ID', how='left')


    # =======================================================
    # 3. PROCESSAMENTO SHOPEE (CLIQUES)
    # =======================================================
    print("Processando relatório de Cliques da Shopee...")
    df_cliques['Sub_id'] = df_cliques['Sub_id'].fillna('').astype(str)
    
    # Quebra a string "MOODBOARD-CARROSSEL---" gerada pela Shopee
    def extrair_subs_clique(sub_str):
        partes = [p for p in sub_str.split('-') if p] # Ignora partes vazias ('---')
        sub1 = partes[0].lower().strip() if len(partes) > 0 else ""
        sub2 = partes[1].lower().strip() if len(partes) > 1 else ""
        return pd.Series([sub1, sub2])
        
    df_cliques[['sub_id1', 'sub_id2']] = df_cliques['Sub_id'].apply(extrair_subs_clique)
    
    # Conta 1 clique para cada linha e agrupa
    shopee_cliques_agrupado = df_cliques.groupby(['sub_id1', 'sub_id2']).size().reset_index(name='Cliques_Shopee')


    # =======================================================
    # 4. PROCESSAMENTO SHOPEE (VENDAS)
    # =======================================================
    print("Processando relatório de Vendas da Shopee...")
    df_vendas['sub_id1'] = df_vendas['Sub_id1'].fillna('').astype(str).str.lower().str.strip()
    df_vendas['sub_id2'] = df_vendas['Sub_id2'].fillna('').astype(str).str.lower().str.strip()
    
    # Aplica a função de limpeza na moeda
    df_vendas['Valor de Compra(R$)'] = df_vendas['Valor de Compra(R$)'].apply(limpar_moeda)
    df_vendas['Comissão líquida do afiliado(R$)'] = df_vendas['Comissão líquida do afiliado(R$)'].apply(limpar_moeda)

    # Soma as compras e valores
    shopee_vendas_agrupado = df_vendas.groupby(['sub_id1', 'sub_id2']).agg(
        Compras_Shopee=('ID do pedido', 'count'),
        Valor_Total_Compras=('Valor de Compra(R$)', 'sum'),
        Comissao_Gerada=('Comissão líquida do afiliado(R$)', 'sum')
    ).reset_index()


    # =======================================================
    # 5. O GRANDE MERGE (CRUZAMENTO DE TODOS OS MUNDOS)
    # =======================================================
    print("Cruzando dados (IG + Shopee Cliques + Shopee Vendas)...")
    
    # Junta os dois universos da Shopee primeiro
    shopee_consolidado = pd.merge(shopee_cliques_agrupado, shopee_vendas_agrupado, on=['sub_id1', 'sub_id2'], how='outer').fillna(0)
    
    # Injeta a Shopee na linha exata do Instagram correspondente
    df_analise = pd.merge(df_analise, shopee_consolidado, on=['sub_id1', 'sub_id2'], how='left').fillna(0)


    # =======================================================
    # 6. CÁLCULOS DOS INDICADORES DE GROWTH (KPIs)
    # =======================================================
    print("Calculando KPIs de Conversão...")
    
    interacoes = df_analise['Curtidas'] + df_analise['Comentarios'] + df_analise['Salvamentos'] + df_analise['Compartilhamentos']
    df_analise['Taxa_Engajamento(%)'] = (interacoes / df_analise['Visualizacoes'].replace(0, np.nan)) * 100
    
    df_analise['Taxa_Conversao(%)'] = (df_analise['Compras_Shopee'] / df_analise['Cliques_Shopee'].replace(0, np.nan)) * 100
    
    df_analise['RPC_por_clique(R$)'] = df_analise['Comissao_Gerada'] / df_analise['Cliques_Shopee'].replace(0, np.nan)
    
    df_analise['RPV_1k_views(R$)'] = (df_analise['Comissao_Gerada'] / df_analise['Visualizacoes'].replace(0, np.nan)) * 1000
    
    df_analise['Ticket_Medio(R$)'] = df_analise['Valor_Total_Compras'] / df_analise['Compras_Shopee'].replace(0, np.nan)

    # Limpeza visual (Arredonda tudo para 2 casas decimais e preenche infinitos com 0)
    df_analise = df_analise.replace([np.inf, -np.inf], np.nan).fillna(0).round(2)

    # =======================================================
    # 7. EXPORTAR RESULTADO
    # =======================================================
    print("Salvando arquivo final Analise_Posts_Dashboard.csv...")
    df_analise.to_csv('data/Analise_Posts_Dashboard.csv', index=False)
    print("Pipeline executado com sucesso!")

if __name__ == "__main__":
    run_pipeline()
