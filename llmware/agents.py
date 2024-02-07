
import shutil
import logging
import gc
import re
import csv
import os
import sqlite3

from llmware.models import ModelCatalog, _ModelRegistry
from llmware.util import CorpTokenizer
from llmware.configs import SQLiteConfig
from llmware.exceptions import ModelNotFoundException


class LLMfx:

    """ LLMfx provides a high-level orchestration abstraction that implements multi-model, multi-step processes
    with the ability to load and orchestrate multiple SLIM classifier models as 'tools' with centralized journaling,
    structured work management and information aggregation.  The initial implementation of LLMfx is designed
    to support SLIM classifier models, with support for additional model classes to come over time. """

    def __init__(self, api_key=None, verbose=True, analyze_mode=True):

        if verbose:
            print("update: Launching LLMfx process")

        self._supported_tools = _ModelRegistry().get_llm_fx_tools_list()
        self._default_tool_map = _ModelRegistry().get_llm_fx_mapping()

        for tools in self._supported_tools:
            setattr(self, tools + "_model", None)

        self.api_key = api_key

        self.work_queue = []
        self.work_iteration = 0

        self.verbose = verbose
        self.analyze_mode = analyze_mode

        #   report is a list of dictionaries, with each dictionary linked to a work item number
        #   reports are automatically aggregated through the lifecycle of the object
        self.report = []

        #   response list provides a list of the llm tool responses
        self.response_list = []

        #   research list provides a list of any research gathered (specifically from SQLTables currently)
        self.research_list = []

        #   journal keeps a running journal output used in 'verbose' mode to the screen display
        self.journal = []
        self.step = 0

        journal_update = f"creating object - ready to start processing."
        self.write_to_journal(journal_update)

        self.tools_deployed = []
        self.inference_calls = 0

    def update_tool_map(self, tool_type, tool_name):

        """ Updates tool mapping for LLMfx instance - enables swapping in other models. """

        if tool_type:
            if tool_type in self._supported_tools:

                # unload tool if currently being used
                self.unload_tool(tool_type)

                # create new mapping
                self._default_tool_map.update({tool_type: tool_name})

                # load new tool
                self.load_tool(tool_type)

        return self

    def clear_work(self):

        """ Detaches any loaded text work and resets the iteration number. """

        self.work_queue = []
        self.work_iteration = 0

        journal_update = f"clearing work queue - reset"
        self.write_to_journal(journal_update)

        return True

    def set_work_iteration(self, num):

        """ Sets the work iteration number. """

        if num < len(self.work_queue):
            self.work_iteration = num

        journal_update = f"setting work iteration to entry - {str(num)}"
        self.write_to_journal(journal_update)

        return True

    def top_of_work_queue(self):

        """ Sets the work iteration number to the last item in the work queue and returns this value. """

        self.work_iteration = len(self.work_queue) - 1
        return self.work_iteration

    def increment_work_iteration(self):

        """ Increments the work iteration - will return None if nothing left in the processing queue. """

        if (self.work_iteration + 1) < len(self.work_queue):
            self.work_iteration += 1
            output_value = self.work_iteration
            journal_update = f"incrementing work iteration to entry - {str(self.work_iteration)}"
        else:
            journal_update = f"completed all work processing"
            output_value = None

        self.write_to_journal(journal_update)

        return output_value

    def _expand_report(self):

        """ Creates an incremental empty report dictionary in line with creation of a new work item. """

        self.report.append({})
        return len(self.report)

    def load_work(self, text, text_key="text"):

        """ Flexible intake method accepts multiple forms of input text:
            --if string, then packages as a dictionary, and adds to the work_queue
            --if dictionary, then checks the keys and adds to the work_queue
            --if list, then unpacks and iterates, adding each entry as a dictionary onto the work queue """

        new_entries_created = 0

        if isinstance(text, str):
            new_entry = {"text": text, "file_source": "NA", "page_num": "NA"}
            self.work_queue.append(new_entry)
            new_entries_created += 1
            self._expand_report()

        if isinstance(text, dict):
            if text_key in text and "file_source" in text and "page_num" in text:
                self.work_queue.append(text)
                new_entries_created += 1
                self._expand_report()
            else:
                if text_key not in text:
                    logging.warning("could not identify dictionary type.")
                    return -1
                else:
                    if "file_source" not in text:
                        text.update({"file_source": "NA"})
                    if "page_num" not in text:
                        text.update({"page_num": "NA"})
                    self.work_queue.append(text)
                    new_entries_created += 1
                    self._expand_report()

        if isinstance(text, list):
            # need to check the type of the entries in the list
            for i, elements in enumerate(text):

                if isinstance(elements, str):
                    new_entry = {"text": elements, "file_source": "NA", "page_num": "NA"}
                    self.work_queue.append(new_entry)
                    new_entries_created += 1
                    self._expand_report()

                if isinstance(elements, dict):
                    if text_key in elements and "file_source" in elements and "page_num" in elements:
                        self.work_queue.append(elements)
                        new_entries_created += 1
                        self._expand_report()
                    else:
                        if text_key not in elements:
                            logging.warning("update: load - skipping - could not identify "
                                            "dictionary type - %s", elements)
                        else:
                            if "file_source" not in elements:
                                elements.update({"file_source": "NA"})
                            if "page_num" not in elements:
                                elements.update({"page_num": "NA"})
                            self.work_queue.append(elements)
                            new_entries_created += 1
                            self._expand_report()

        journal_update = f"loading new processing text - {str(new_entries_created)} new entries"
        self.write_to_journal(journal_update)

        return self.work_queue

    def clear_state(self):

        """ Resets key state variables of LLMfx instance """

        self.journal = []
        self.tools_deployed = []
        self.inference_calls = 0
        self.response_list = []
        # self.report = {}
        self.report = []
        self.step = 0

        return self

    def activity_summary(self):

        """ Provides an activity summary and writes to journal. """

        activity_summary = {"inference_count": self.inference_calls, "tools_used": len(self.tools_deployed),
                            "tools": self.tools_deployed}

        journal_update = f"generating activity_summary - {str(activity_summary)}"
        self.write_to_journal(journal_update)

        return activity_summary

    def show_report(self, iteration_num=None,add_source=True):

        """ Shows the gathered report so far, and writes to journal. """

        output_report = []

        if iteration_num:

            if not isinstance(iteration_num,list):
                iteration_num = [iteration_num]

            # show specific report(s)
            journal_update = f"showing selected reports - {str(iteration_num)}\n"
            for n in iteration_num:
                journal_update += f"showing gathered report - {str(self.report[n])}\n"
                for key, value in self.report[n].items():
                    journal_update += f"\t\t\t\t -- {key.ljust(20)} - {str(value).ljust(40)}\n"

                base_report = self.report[n]
                if add_source:
                    base_report.update({"source": self.work_queue[n]})

                self.write_to_journal(journal_update)
                output_report.append(base_report)

        else:
            # show all reports
            output_report = []
            journal_update = f"showing all gathered reports - {str(self.report)}\n"
            for i, entries in enumerate(self.report):
                journal_update += f"report - {str(i)} - {str(self.report[i])}\n"
                for key, value in self.report[i].items():
                    journal_update += f"\t\t\t\t -- {key.ljust(20)} - {str(value).ljust(40)}\n"
                if add_source:
                    entries.update({"source": self.work_queue[i]})
                output_report.append(entries)
            self.write_to_journal(journal_update)
            # output_report = self.report

        return output_report

    def lookup_response_by_tool(self, tool_type):

        """ Looks up an item in the response list by tool type. """

        output = []

        for i, response in enumerate(self.response_list):
            if response["tool"] == tool_type:
                output.append(response)

        return output

    def follow_up_list(self, key=None, value=None):

        """ Analyzes response list and returns sub-set with matching 'key' and 'value' """

        follow_up_list = []

        if not key:
            journal_update = f"building follow-up_list - looking for distinct work items\n"
        else:
            journal_update = f"building follow_up_list - looking for {key} - {value}\n"

        key_value_str = f"{key} - {value}"

        for i, response in enumerate(self.response_list):
            if "llm_response" in response:

                work_num = response["work_iteration"]
                text = response["text"]

                if key:
                    if key in response["llm_response"]:
                        if value in response["llm_response"][key]:
                            follow_up_list.append(work_num)

                            journal_update += f"\t\t\t\t -- {key_value_str.ljust(20)} - {str(work_num)} - {str(text)}\n"

                else:
                    if work_num not in follow_up_list:
                        follow_up_list.append(work_num)
                        placeholder = "distinct_work_item"
                        journal_update += f"\t\t\t\t -- {placeholder.ljust(20)} - {str(work_num)} - {str(text)}\n"

        self.write_to_journal(journal_update)

        return follow_up_list

    def analyze_responses(self, key,value):

        """ Analyzes response list and returns sub-set with matching 'key' and 'value' """

        journal_update = f"analyzing responses - looking for {key} - {value}\n"

        output_list = []
        key_value_str = f"{key} - {value}"

        for i,response in enumerate(self.response_list):
            if "llm_response" in response:
                if key in response["llm_response"]:
                    if value in response["llm_response"][key]:
                        output_list.append(response)

                        cl = response["confidence_score"]
                        text = response["work_item"]["text"]
                        step = response["step"]

                        journal_update += f"\t\t\t\t -- {key_value_str.ljust(20)} - {str(step)} - {str(text)}\n"

        self.write_to_journal(journal_update)

        return output_list

    def load_tool(self, tool_type):

        """ Loads a single tool """

        model = None
        if tool_type in self._supported_tools:

            journal_update = f"loading tool - {tool_type}"
            self.write_to_journal(journal_update)

            setattr(self, tool_type + "_model",
                    ModelCatalog().load_model(self._default_tool_map[tool_type],api_key=self.api_key))

            model = getattr(self, tool_type + "_model")

            if tool_type not in self.tools_deployed:
                self.tools_deployed.append(tool_type)

        return model

    def load_tool_list(self, tool_list):

        """ Loads a list of tool, typically at the start of a multi-step process. """

        for tool_type in tool_list:

            if tool_type in self._supported_tools:

                model = getattr(self, tool_type + "_model")

                if not model:
                    self.load_tool(tool_type)

        return self

    def unload_tool(self, tool_type):

        """ Unloads a tool, which removes it from memory - useful in long-running processes
        to be able to load and unload different tools. """

        if tool_type in self._supported_tools:

            journal_update = f"unloading tool - {tool_type}"
            self.write_to_journal(journal_update)

            model = getattr(self, tool_type + "_model")
            model.unload_model()

            delattr(self, tool_type + "_model")
            setattr(self, tool_type + "_model", None)
            gc.collect()

        return 0

    def write_to_journal(self, journal_update):

        """ Adds an event to the running journal list and displays if in verbose mode. """

        self.journal.append(journal_update)
        self.step += 1

        if self.verbose:
            print(f"step - \t{str(self.step)} - \t{journal_update}")

        return True

    def exec_function_call(self, tool_type, text=None, function="classify", params=None, get_logits=True):

        """ Executes a function call on the selected tool type. """

        value_output = {}

        if tool_type in self._supported_tools:

            model = getattr(self, tool_type + "_model")

            #   if model not yet loaded, then load in-line
            if not model:
                model = self.load_tool(tool_type)

            function_call = getattr(model, "function_call")

            journal_update = f"executing function call - deploying - {tool_type} "
            self.write_to_journal(journal_update)

            if text:
                # if text passed directly, then add to work queue
                self.load_work(text)
                # set work iteration to be the last item
                self.top_of_work_queue()

            #   pull from the work queue
            work_dict = self.work_queue[self.work_iteration]
            work_iter = self.work_iteration
            text = work_dict["text"]

            if not self.analyze_mode:
                get_logits = False

            response = function_call(text, function=function, params=params, get_logits=get_logits)

            self.inference_calls += 1
            output_response = {}
            logit_analysis = {}

            if response:

                if "llm_response" in response:

                    llm_response = response["llm_response"]
                    output_type = response["usage"]["type"]
                    usage= response["usage"]

                    if response["usage"]["type"] == "dict":
                        dict_output = True
                        self.report[work_iter] = self.report[work_iter] | response["llm_response"]

                    else:
                        logging.warning("update: could not automatically convert to dictionary - "
                                        "keeping as string output")
                        dict_output = False

                    # assemble output
                    value_output.update({"llm_response": llm_response,"dict_output": dict_output})

                    # start journaling update
                    journal_update = f"executing function call - " \
                                     f"getting response - {tool_type}\n"
                    journal_update += f"\t\t\t\t -- llm_response - {str(llm_response)}\n"
                    journal_update += f"\t\t\t\t -- output type - {output_type}\n"
                    journal_update += f"\t\t\t\t -- usage - {usage}"

                    self.write_to_journal(journal_update)
                    # end journaling

                    # default - if not found/applied
                    confidence_score = -1

                    if get_logits:
                        logit_analysis = ModelCatalog().logit_analysis(response, model.model_card,
                                                                       model.hf_tokenizer_name,
                                                                       api_key=self.api_key)

                        confidence_score = logit_analysis["confidence_score"]
                        ryg = logit_analysis["ryg_string"]
                        choices = logit_analysis["choices"]
                        marker_tokens = logit_analysis["marker_tokens"]
                        output_response.update({"logit_analysis": logit_analysis})

                        # start journaling update
                        journal_update = f"analyzing response - {tool_type}\n"
                        journal_update += f"\t\t\t\t -- confidence score - {str(confidence_score)}\n"
                        journal_update += f"\t\t\t\t -- analyzing response - {ryg}\n"
                        journal_update += f"\t\t\t\t -- analyzing response - {choices}"
                        if marker_tokens:
                            journal_update += "\n"
                            journal_update += f"\t\t\t\t -- analyzing response - {str(marker_tokens)}"

                        self.write_to_journal(journal_update)

                        value_output.update({"confidence_score": confidence_score})
                        if marker_tokens:
                            value_output.update({"choices": marker_tokens})

                    # assemble output response dictionary

                    output_response = {"step": self.step, "tool": tool_type, "inference": self.inference_calls,
                                       "llm_response": llm_response}

                    if get_logits:
                        output_response.update({"confidence_score": confidence_score})

                    output_response.update({"llm_usage": usage, "work_iteration": work_iter, "dict_output": dict_output})

                    for keys, values in work_dict.items():
                        output_response.update({keys:values})

                    if get_logits:
                        output_response.update({"logit_analysis": logit_analysis})

                    # save to response list state tracker
                    self.response_list.append(output_response)

        else:
            raise ModelNotFoundException(tool_type)

        # print("update: output_response - ", output_response)

        # replacing output_response with value_output which is a dictionary "subset" of the full output response

        return value_output

    def exec_multitool_function_call(self, tool_type_list, text=None, function="classify", params=None,
                                     get_logits=True):

        """ Executes multiple function calls on the same text with a list of tools in tool_type_list """

        output_list = []

        for tool_type in tool_type_list:

            response = self.exec_function_call(tool_type,text=text,get_logits=get_logits,
                                               params=params, function=function)

            output_list.append(response)

        return output_list

    def sentiment(self, text=None, params=None):

        """ Executes sentiment analysis on text, if passed directly, or will pull current work item from the
         queue.  Returns value output dictionary with sentiment classification, confidence score and choices. """

        if not params:
            # default parameter key
            params = ["sentiment"]

        if isinstance(params, str):
            params = [params]

        return self.exec_function_call("sentiment", text=text, params=params)

    def topics(self, text=None, params=None):

        """ Executes topics analysis on text, if passed directly, or will pull current work item from the queue.
        Returns value output dictionary with topics classification and confidence score. """

        if not params:
            # default parameter key
            params = ["topic"]

        if isinstance(params, str):
            params = [params]

        return self.exec_function_call("topics", text=text, params=params)

    def named_entity_extraction(self, text=None, params=None):

        """ Executes named entity classification analysis on a text, if passed directly, or will pull current
        work item from the queue.   Returns value output dictionary with named entity classification and
        confidence score. """

        if not params:
            # default parameter key
            params = ["people", "place", "company", "misc"]

        if isinstance(params, str):
            params = [params]

        return self.exec_function_call("ner", text=text, params=params)

    def ner(self, text=None, params=None):

        """ Executes named entity classification analysis on a text, if passed directly, or will pull current
        work item from the queue.   Returns value output dictionary with named entity classification and
        confidence score. """

        #TODO: identical to "named_entity_extraction" method - should remove one of them

        if not params:
            # default parameter key
            params = ["people", "place", "company", "misc"]

        if isinstance(params, str):
            params = [params]

        return self.exec_function_call("ner", text=text, params=params)

    def ratings(self, text=None, params=None):

        """ Executes ratings classification analysis on a text of 1-5, if passed directly, or will pull current
        work item from the queue.   Returns value output dictionary with rating classification and
        confidence score. """

        if not params:
            # default parameter key
            params = ["rating"]

        if isinstance(params, str):
            params = [params]

        return self.exec_function_call("ratings", text=text, params=params)

    def emotions(self, text=None, params=None):

        """ Executes emotions classification analysis on a text, if passed directly, or will pull current
        work item from the queue.   Returns value output dictionary with emotions classification and
        confidence score. """

        if not params:
            # default parameter key
            params = ["emotions"]

        if isinstance(params, str):
            params = [params]

        return self.exec_function_call("emotions", text=text, params=params)

    def intent(self, text=None, params=None):

        """ Executes intent classification analysis on a text, if passed directly, or will pull current
        work item from the queue.   Returns value output dictionary with intent classification and
        confidence score. """

        if not params:
            # default parameter key
            params = ["intent"]

        if isinstance(params, str):
            params = [params]

        return self.exec_function_call("intent", text=text, params=params)

    def tags(self, text=None, params=None):

        """ Generates a list of relevant 'tag' information data points from a text, if passed directly, or
        will pull current work item from the queue.   Returns value output dictionary with list of key
        highlighted points. """

        if not params:
            # default parameter key
            params = ["tags"]

        if isinstance(params, str):
            params = [params]

        return self.exec_function_call("tags", text=text, params=params)

    def nli(self, text1, text2, params=None):

        """ Executes a natural language inference classification on a text, if passed directly, or will pull current
        work item from the queue.   Returns value output dictionary with the NLI classification and
        confidence score. """

        if not params:
            # default parameter key
            params = ["evidence"]

        if isinstance(params, str):
            params = [params]

        context = "Evidence: " + text1 + "\n" + "Conclusion: " + text2

        return self.exec_function_call("nli", text=context, params=params)

    def verify_llm_response(self, input_context, llm_response):

        """ Utility function to apply NLI to compare llm_response with the input context. """

        return self.nli(input_context, llm_response)

    def answer(self, question, context=None, key=None):

        """ Executes an inference """

        model = getattr(self, "answer" + "_model")
        inference = getattr(model, "inference")

        journal_update = f"executing function call - deploying - question-answer tool "
        self.write_to_journal(journal_update)

        if context:
            self.load_work(context)

        work_dict = self.work_queue[self.work_iteration]
        text = work_dict["text"]
        work_iter = self.work_iteration

        response = inference(question, add_context=text, add_prompt_engineering=True)

        llm_response = re.sub("[\n\r]", "\t", response["llm_response"])

        if not key:
            self.report[work_iter].update({"answer": [llm_response]})
            answer_key = "answer"
        else:
            self.report[work_iter].update({key:[llm_response]})
            answer_key = key

        usage = response["usage"]

        self.inference_calls += 1

        # start journaling update
        journal_update = f"executing function call - " \
                         f"getting response - question - {answer_key}\n"
        journal_update += f"\t\t\t\t -- llm_response - {str(llm_response)}\n"
        journal_update += f"\t\t\t\t -- output type - text\n"
        journal_update += f"\t\t\t\t -- usage - {usage}"

        self.write_to_journal(journal_update)

        # assemble output response dictionary

        output_response = {"step": self.step, "tool": "answer", "inference": self.inference_calls,
                           "llm_response": llm_response}

        get_logits=False

        if get_logits:
            confidence_score =-1
            output_response.update({"confidence_score": confidence_score})

        output_response.update({"llm_usage": usage, "work_iteration": work_iter, "dict_output": False})

        for keys, values in work_dict.items():
            output_response.update({keys:values})

        if get_logits:
            logit_analysis= {}
            output_response.update({"logit_analysis": logit_analysis})

        # save to response list state tracker
        self.response_list.append(output_response)

        return output_response

    def sql(self, query, table_schema):

        """ Executes Text2Sql tool to convert query into SQL """

        model = getattr(self, "sql" + "_model")
        inference = getattr(model, "inference")

        if table_schema:
            self.load_work(table_schema)
            self.top_of_work_queue()

        work_dict = self.work_queue[self.work_iteration]
        table_schema = work_dict["text"]
        work_iter = self.work_iteration

        # initial journal update
        journal_update = f"executing function call - deploying - text-to-sql\n"
        journal_update += f"\t\t\t\t -- query - {query}\n"
        journal_update += f"\t\t\t\t -- table_schema - {table_schema}"
        self.write_to_journal(journal_update)

        response = inference(query, add_context=table_schema, add_prompt_engineering=True)

        self.inference_calls += 1

        llm_response = response["llm_response"]

        self.report[work_iter].update({"sql": [llm_response]})

        usage = response["usage"]

        self.inference_calls += 1

        # start journaling update
        journal_update = f"executing function call - getting response - sql\n"
        journal_update += f"\t\t\t\t -- llm_response - {str(llm_response)}\n"
        journal_update += f"\t\t\t\t -- output type - text\n"
        journal_update += f"\t\t\t\t -- usage - {usage}"

        self.write_to_journal(journal_update)
        # end journaling

        # assemble output response dictionary

        output_response = {"step": self.step, "tool": "sql", "inference": self.inference_calls,
                           "llm_response": llm_response}

        #   logits not yet activated for inference calls - TBD - set 'get_logits = False" for now
        get_logits=False
        if get_logits:
            confidence_score =-1
            output_response.update({"confidence_score": confidence_score})

        output_response.update({"llm_usage": usage, "work_iteration": work_iter, "dict_output": False})

        for keys, values in work_dict.items():
            output_response.update({keys:values})

        if get_logits:
            logit_analysis= {}
            output_response.update({"logit_analysis": logit_analysis})

        # save to response list state tracker
        self.response_list.append(output_response)

        return output_response

    def query_db(self, query, table=None, table_schema=None, db=None, db_name=None):

        """ Executes two steps - converts input query into SQL, and then executes the SQL query on the DB. """

        sql_db = SQLTables(db=db, db_name=db_name)

        if not table_schema:
            if table:
                table_schema = sql_db.get_table_schema(table)

        # step 1 - convert question into sql

        if not table_schema:
            logging.warning("update: LLMfx - query_db - could not identify table schema - can not proceed")
            return -1

        # run inference with query and table schema to get SQL query response
        response = self.sql(query, table_schema)

        # step 2 - run query
        sql_query = response["llm_response"]
        sql_db_name = sql_db.db_file

        # initial journal update
        journal_update = f"executing research call - executing query on db\n"
        journal_update += f"\t\t\t\t -- db - {sql_db_name}\n"
        journal_update += f"\t\t\t\t -- sql_query - {sql_query}"
        self.write_to_journal(journal_update)

        db_output = sql_db.query_db(response["llm_response"])

        output = []
        db_response = list(db_output)

        for rows in db_response:
            output.append(rows)

        result = {"step": self.step, "tool": "sql", "db_response": output, "sql_query": response["llm_response"],
                  "query": query,"db": sql_db_name, "work_item": table_schema}

        self.research_list.append(result)

        # start journaling update
        journal_update = f"executing research  - getting response - sql\n"
        journal_update += f"\t\t\t\t -- result - {str(output)}"
        # journal_update += f"\t\t\t\t -- output type - text"

        self.write_to_journal(journal_update)
        # end journaling

        return result

    def token_comparison (self, value_string, context):

        """ Utility function to perform token-level comparison in llm_response with input source materials. """

        # note: this is a more limited version of the QualityCheck tools used in Prompt class

        c = CorpTokenizer(remove_stop_words=True, remove_numbers=False,
                          one_letter_removal=True, remove_punctuation=False)

        llm_response_tokens = c.tokenize(value_string)
        context_tokens = c.tokenize(context)

        # iterate thru each key point and analyze comparison match
        matched = []
        unmatched = []

        for i, tok in enumerate(llm_response_tokens):

            if tok.endswith("."):
                tok = tok[:-1]

            if tok.endswith(";"):
                tok = tok[:-1]

            tok = re.sub("[,();$\"\n\r\t\u2022\u201c\u201d]", "", tok)

            if len(tok) > 0:

                match_found = False

                for j, etoks in enumerate(context_tokens):

                    if etoks.endswith("."):
                        etoks = etoks[:-1]

                    if etoks.endswith(";"):
                        etoks = re.sub("[(),;$\n\r\t\"\u2022\u201c\u201d]", "", etoks)

                    if tok == etoks:
                        # found matching token
                        match_found = True
                        matched.append(tok)
                        break

                if not match_found:
                    unmatched.append(tok)

        # match_percent = 0.0
        match_percent = "{0:.1f}%".format(0.0)
        match_fr = 0.0

        if (len(matched) + len(unmatched)) > 0:

            match_fr = len(matched) / (len(matched) + len(unmatched))

            if match_fr > 1.0:
                match_fr = 1.0

            match_percent = "{0:.1f}%".format((match_fr * 100))

        comparison_stats = {"percent_display": match_percent,
                            "confirmed_words": matched,
                            "unconfirmed_words": unmatched,
                            "verified_token_match_ratio": match_fr,
                            }

        return comparison_stats


class SQLTables:

    """ SQLTables is a class for creating and accessing external SQL data, primarily as a resource that is
    accessible via Text2SQL programmatic inferences.

    This is an **experimental** feature, and currently supports only use of SQLite, configured as a separate
    local file-based DB, e.g., sqlite-experimental.db

    Use of this class will create a separate sqlite_experimental.db per the configs in SQLiteConfig
    """

    def __init__(self, db=None, db_name=None, experimental=True):

        self.db = "sqlite"

        # default config for "db_experimental" = "sqlite_experimental.db"
        self.db_name = SQLiteConfig().get_config("db_experimental")

        if experimental:
            self.db_file = SQLiteConfig().get_uri_string_experimental_db()
            logging.warning("update: connecting to experimental sqlite db - %s", self.db_file)

        else:
            self.db_file = SQLiteConfig().get_uri_string()
            logging.warning("warning: connecting to main sqlite db - %s", self.db_file)

        self.conn = sqlite3.connect(self.db_file)

        self.tables = []

    def get_table_schema(self,table_name):

        """ Lookup of table_schema for an input table_name - outputs 'create table schema string' that can
        be used directly as context in a text2sql inference """

        table_schema = ""

        sql_query = f"SELECT * FROM sqlite_master WHERE type = 'table' AND name = '{table_name}';"

        table_schema_row = self.conn.cursor().execute(sql_query)
        table_schema_row = list(table_schema_row)

        if len(table_schema_row) > 0:
            table_schema = table_schema_row[0][4]

        return table_schema

    def get_column_names(self, table_name):

        """ Gets the column names from a table, and provides a list as output. """

        column_names = []

        sql_query_pragma = "PRAGMA table_info('{}')".format(table_name)
        column_info = self.conn.cursor().execute(sql_query_pragma)

        for entries in column_info:
            # print("pragma - columns info output - ", entries)
            column_names.append(entries[1])

        return column_names

    def query_db(self, sql_query):

        """ Executes a query directly on database """

        # note: security and access are left to the user to manage

        try:
            result = self.conn.cursor().execute(sql_query)
        except:
            logging.warning("update: query generated error - not successful - %s", sql_query)
            result = None

        return result

    def delete_experimental_db(self, confirm_delete=False):

        """ Deletes the experimental db """

        # delete db and start fresh
        if confirm_delete:
            shutil.rmtree(self.db_file)
            logging.warning("update: deleted sqlite experimental db - %s ", self.db_file)

        return True

    def delete_table(self, table_name, confirm_delete=False):

        """ Deletes a table on the experimental db """

        if confirm_delete:

            sql_instruction = f"DROP TABLE {table_name};"
            results = self.conn.cursor().execute(sql_instruction)
            self.conn.commit()
            logging.warning("update: delete sqlite experimental db - table - %s ", table_name)

        return 0

    def register_table(self, sql_table_create):
        self.tables.append(sql_table_create)
        return self.tables

    def reset_tables(self):
        self.tables = []
        return True

    def table_exists_check(self, table_name):

        """Checks if table exists - true if exists, false if does not exist. """

        sql_query = f"SELECT * FROM sqlite_master WHERE type = 'table' AND name = '{table_name}';"

        results = self.conn.cursor().execute(sql_query)

        if len(list(results)) > 0:
            table_exists = True
        else:
            table_exists = False

        return table_exists

    def load_csv(self, fp, fn):

        """ Opens CSV file at folder_path fp and file_name fn and returns array-like output in memory """

        in_path = os.path.join(fp,fn)
        record_file = open(in_path, encoding='utf-8')# encoding='ISO-8859-1', )
        c = csv.reader(record_file, dialect='excel', doublequote=False, delimiter=',')
        output = []
        for lines in c:
            output.append(lines)
        record_file.close()

        return output

    def create_new_table(self, output, table_name):

        """ Creates a new table, deriving the column names from an implied header row in the output,
        and a sniff test on the value types. """

        col_names = []

        if len(output) > 1:
            header_row = output[0]
            test_row = output[1]

        keys_list = "("

        sql_create_table = f"CREATE TABLE {table_name} ("
        for i, entry in enumerate(header_row):
            col_name = re.sub("[\xfe\xff]","",entry)
            try:
                test_int = int(test_row[i])
                type="integer"
            except:
                type="text"

            col_names.append(col_name)

            keys_list += col_name + ", "

            sql_create_table += col_name + " " + type + ", "

        if sql_create_table.endswith(", "):
            sql_create_table = sql_create_table[:-2]

        sql_create_table += " )"

        if keys_list.endswith(", "):
            keys_list = keys_list[:-2]

        keys_list += " )"

        self.conn.cursor().execute(sql_create_table)

        return col_names

    def insert_new_row(self, table_name, keys_list, new_row):

        """ Inserts a new row into table. """

        col_names = "("
        for cols in keys_list:
            col_names += cols + ", "
        if col_names.endswith(", "):
            col_names = col_names[:-2]
        col_names += ")"

        values_list = "("
        for j in range(0, len(new_row)):
            values_list += "$" + str(j + 1) + ", "

        if values_list.endswith(", "):
            values_list = values_list[:-2]

        values_list += ")"

        new_record = f"INSERT INTO {table_name} {col_names} VALUES {values_list};"

        logging.info("update: inserting new_record - %s ", new_record)

        self.conn.cursor().execute(new_record, new_row)

        return True

    def create_new_table_from_csv(self,fp=None, fn=None, table_name=None):

        """ Designed for rapid prototyping - input is a well-formed csv file with assumed header row with
        each entry representing a column name, and well-formed rows. """

        #   load csv
        output = self.load_csv(fp,fn)

        #   check if table exists
        if not self.table_exists_check(table_name):

            logging.info("update: table does not exist - so creating")
            # need to build the table
            column_names = self.create_new_table(output, table_name)
            logging.info("update: table created - column names - %s ", column_names)

        else:
            print("update: table exists - getting column names")
            column_names = self.get_column_names(table_name)

        # insert records

        new_record = ""
        for i in range(1, len(output)):

            # print("update: inserting new record - ", i, output[i])

            self.insert_new_row(table_name,column_names,output[i])

        self.conn.commit()
        self.conn.close()

        logging.info("update: done inserting records into new table")

        return 0





