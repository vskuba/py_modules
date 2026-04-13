import httpx
from bs4 import BeautifulSoup


async def web_read_page(url: str) -> str:
    """
    Opens a URL and returns the text content of the page.
    Use this to get information from the internet.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(url, follow_redirects=True)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, 'html.parser')

            # Удаляем мусор (скрипты, стили)
            for script_or_style in soup(["script", "style", "header", "footer", "nav"]):
                script_or_style.decompose()

            # Получаем текст и убираем лишние пробелы
            text = soup.get_text(separator='\n')
            clean_text = "\n".join([line.strip() for line in text.splitlines() if line.strip()])

            # Ограничиваем объем, чтобы не превысить контекстное окно LLM
            return clean_text[:5000]
        except Exception as e:
            return f"Error reading page {url}: {str(e)}"
