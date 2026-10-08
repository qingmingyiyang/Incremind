"""本机文字向量的版本化配置，不依赖存储或模型运行时。"""


def v1():
    return {
        'model': 'google/embeddinggemma-2', 'dims': 256,
        'query_prefix': 'task: search result | query: ',
        'document_prefix': 'title: {title} | text: {content}',
        'max_input_chars': 24000, 'max_items': 128,
        'batch_size': 8, 'queue_limit': 64,
    }
