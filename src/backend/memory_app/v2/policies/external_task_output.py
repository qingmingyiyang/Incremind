def v1():
    return {
        'version': 'external_task_output@1',
        'directory': 'outputs',
        'instruction': '将要交付的 UTF-8 文本或代码文件保存到 outputs/，并在最终消息中列出相对文件名及任务结果。',
        'max_files': 32,
        'max_file_bytes': 200_000,
        'max_total_bytes': 4 * 1024 * 1024,
        'text_suffixes': ['.txt', '.md', '.csv', '.json', '.yaml', '.yml', '.toml', '.ini',
            '.cfg', '.py', '.js', '.jsx', '.ts', '.tsx', '.html', '.css', '.sql', '.sh',
            '.ps1', '.xml', '.svg', '.r', '.rs', '.go', '.java', '.c', '.h', '.cpp', '.hpp'],
    }
