import subprocess
import tempfile
from langchain.callbacks.streaming_stdout import StreamingStdOutCallbackHandler
from langchain.chat_models.openai import ChatOpenAI
from langchain.schema import HumanMessage, AIMessage
from langchain.embeddings.openai import OpenAIEmbeddings
from langchain.text_splitter import MarkdownTextSplitter
from langchain.text_splitter import CharacterTextSplitter
from langchain.text_splitter import TokenTextSplitter
from langchain.utilities import GoogleSearchAPIWrapper
from langchain.callbacks import get_openai_callback
from langchain.callbacks.base import CallbackManager
from langchain.callbacks.streaming_stdout import StreamingStdOutCallbackHandler
from dotenv import load_dotenv
from html2text import HTML2Text
import numpy as np
import tiktoken
import json
import requests
import re
import logging
import ssl
import readline
from typing import Optional

load_dotenv()


# Utils


def count_tokens(text: str) -> int:
    encoding = tiktoken.encoding_for_model("gpt-4")
    tokens = encoding.encode(text)
    return len(tokens)


def request(url: str) -> list[str]:
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/50.0.2661.102 Safari/537.36'}
    resp = requests.get(url, headers=headers).text
    h = HTML2Text()
    h.ignore_links = True
    h.ignore_images = True
    markdown = h.handle(resp)
    # text_splitter = CharacterTextSplitter(
    #     chunk_size=200, chunk_overlap=0, length_function=count_tokens)
    text_splitter = MarkdownTextSplitter(
        chunk_size=200, chunk_overlap=0, length_function=count_tokens)
    # text_splitter = TokenTextSplitter(
    #     chunk_size=200, chunk_overlap=0)
    docs = text_splitter.split_text(markdown)
    return docs


def vector_similarity(x: list[float], y: list[float]) -> float:
    return np.dot(np.array(x), np.array(y))


def top_k_similar_docs(query: str, docs: list[str], k: int = 5) -> list[str]:
    embeddings = OpenAIEmbeddings(model="text-embedding-ada-002")
    query_embedding = embeddings.embed_query(query)
    doc_embeddings = embeddings.embed_documents(docs)
    similarities = [vector_similarity(
        query_embedding, doc_embedding) for doc_embedding in doc_embeddings]
    top_k = np.flip(np.argsort(similarities)[-k:])
    return [docs[i] for i in top_k]


def run_with_timeout(cmd, timeout_sec):
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        try:
            outs, errs = proc.communicate(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            proc.kill()
            outs, errs = proc.communicate()
            raise TimeoutError(
                f"Process took too long (>{timeout_sec} seconds)")

    if errs:
        return errs.decode('utf-8')
    else:
        return outs.decode('utf-8')


# Tools


links = []


def search(queries: str) -> str:
    summary = ''
    for query in json.loads(queries):
        results = GoogleSearchAPIWrapper().results(query, 5)
        for result in results:
            i = len(links)
            summary += f'[{i}] {result["title"]}\n{result.get("snippet", "")}\n'
            links.append({"link": result["link"], "query": query})
    logging.info(links)
    return summary


def summarize(snippet_ids: str) -> str:
    summary = ''
    for id in json.loads(snippet_ids):
        try:
            docs = request(links[id]["link"])
            top_k = top_k_similar_docs(links[id]["query"], docs, 2)
            summary += f'[{id}]\n'
            summary += '\n'.join(top_k)
            summary += '\n'
        except Exception as e:
            logging.error(e)
            continue
    return summary


def python(code: str) -> str:
    pattern = r'(?<=("""))(.|\n)*?(?=\1)'
    match = re.search(pattern, code).group(0)
    try:
        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(match.encode())
            tmp.flush()
            return run_with_timeout(['python', tmp.name], timeout_sec=5)
    except Exception as e:
        logging.error(e)
        print(e)
        return str(e) + "\ntry again and optimize the code"


tools = [
    {"name": "SEARCH", "args": "(queries: string[])",
     "description": "searches the web, and returns the top snippets, it'll be better if the query string is in english", "run": search},
    {"name": "SUMMARIZE", "args": "(snippet_ids: uint[])",
     "description": "click into the search result, useful when you want to investigate the detail of the search result", "run": summarize},
    {"name": "PYTHON", "args": "(code: string)",
     "description": "evaluates the code in a python interpreter, wrap code in triple quotes, wrap the answer in `print()`", "run": python},
]


# LLM


messages = []


def add_message(message, is_tool_result: bool = False):
    global messages
    messages.append((is_tool_result, message))


def clear_messages():
    global messages
    messages = []


def remove_intermediate_messsages():
    global messages
    messages = list(filter(lambda msg: not msg[0], messages))


def messages_tokens() -> int:
    return sum(map(lambda msg: count_tokens(msg[1].content), messages))


def call_llm(streaming: bool = False) -> str:
    if streaming:
        chat = ChatOpenAI(model_name="gpt-4", streaming=True, callback_manager=CallbackManager(
            [StreamingStdOutCallbackHandler()]), verbose=True, temperature=0)
    else:
        chat = ChatOpenAI(model_name="gpt-4", verbose=True, temperature=0)
    logging.info(f"gpt-context: {messages}")
    resp = chat.generate([(map(lambda msg: msg[1], messages))]).generations[0][0].text
    logging.info(f"gpt-response: {resp}")
    prompt_tokens = messages_tokens()
    complete_tokens = count_tokens(resp)
    total_cost = (0.03 * prompt_tokens + 0.06 * complete_tokens) / 1000
    logging.info(
        f"cost: ${total_cost}, prompt_tokens: {prompt_tokens}, complete_tokens: {complete_tokens}")
    print('')
    return resp
    

# Prompts
    

def instruction_prompt(query: str, tools: list[dict], context: Optional[str] = None) -> str:
    prompt = "You are an helpful and kind assistant to answer questions that can use tools to interact with real world and get access to the latest information. You can call one of the following functions:\n"

    for tool in tools:
        prompt += f'- {tool["name"]}{tool["args"]} {tool["description"]}\n'

    prompt += "In each response, you must start with a function call like `SEARCH(\"something\")` or `PYTHON(\"\"\"1+1\"\"\")`. Don't explain why you use a tool. If you cannot figure out the answer, you say ’I don’t know’. When you are generating answers according to the search result, link your answers to the snippet id like `[1]`, and use the same language as the questioner\n"

    if context:
        prompt += "Context from the previous assistant:\n```\n"
        prompt += context
        prompt += "\n```\n"

    prompt += f"Q:{query}"
    return prompt


def summarize_messages() -> str:
    add_message(HumanMessage(content="Summarize the conversations above for another assistant to continue the process"), is_tool_result=True)
    return call_llm(streaming=False)


# The REPL loop


references = []


def add_reference(answer: str):
    global references
    pattern = r'\[(\d+)\]'
    matched = re.findall(pattern, answer)
    for id in matched:
        if int(id) not in references:
            references.append(int(id))


def show_references():
    global references
    output = ""
    for id in references:
        output += f"[{id}]: {links[id]['link']}\n"
    print(output)
    references = []


def run(query: str) -> str:
    if len(messages) == 0:
        add_message(HumanMessage(content=instruction_prompt(query, tools)))
    else:
        remove_intermediate_messsages()
        if messages_tokens() < 1100:
            add_message(HumanMessage(content=f"Q:{query}"))
        else:
            context = summarize_messages()
            logging.info(f"summarization: {context}")
            clear_messages()
            add_message(HumanMessage(
                content=instruction_prompt(query, tools, context)))

    while True:
        resp = call_llm(streaming=True)
        add_message(AIMessage(content=resp))
        add_reference(resp)
        pattern = r'(\w+)\(([\s\S]*)\)'
        match = re.search(pattern, resp)
        if match:
            func_name = match.group(1)
            func_args = match.group(2)
            for tool in tools:
                if tool["name"] == func_name:
                    result = tool["run"](func_args)
                    result = f"```result\n{result}\n```"
                    logging.info(f"tool-result: {result}")
                    add_message(AIMessage(content=result), is_tool_result=True)
                    break
            else:
                logging.info("no function call, so it is the answer")
                return resp
        else:
            logging.info("no function call, so it is the answer")
            return resp


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s:%(message)s',
        handlers=[
            logging.FileHandler("gpt-search.log"),
            # logging.StreamHandler()
        ]
    )
    while True:
        user_input = input("> ")
        logging.info(f"user-input: {user_input}")
        try:
            run(user_input)
            show_references()
        except Exception as e:
            print("Error:", e)
