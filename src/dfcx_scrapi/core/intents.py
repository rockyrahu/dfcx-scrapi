"""Intent Resource functions."""

# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from collections import defaultdict
import json
import logging
from typing import Dict, List, Tuple
import pandas as pd

from google.cloud.dialogflowcx_v3beta1 import services
from google.cloud.dialogflowcx_v3beta1 import types
from google.protobuf import field_mask_pb2

from dfcx_scrapi.core.scrapi_base import ScrapiBase

# logging config
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


class Intents(ScrapiBase):
    """Core Class for CX Intent Resource functions."""

    def __init__(
        self,
        creds_path: str = None,
        creds_dict: Dict = None,
        creds=None,
        scope=False,
        intent_id: str = None,
        agent_id: str = None,
    ):
        super().__init__(
            creds_path=creds_path,
            creds_dict=creds_dict,
            creds=creds,
            scope=scope,
        )

        if intent_id:
            self.intent_id = intent_id
            self.client_options = self._set_region(self.intent_id)

        if agent_id:
            self.agent_id = agent_id


    @staticmethod
    def intent_proto_to_dataframe(
        obj: types.Intent, mode: str = "basic"
    ) -> pd.DataFrame:
        """Single intent to pandas DataFrame.

        Args:
          obj (types.Intent)
          mode (str):
            "basic" returns display name and training phrase as plain text.
            "advanced" returns training phrases broken out by parts
            with their parameters included.
        """
        if not isinstance(obj, types.Intent):
            raise ValueError("obj should be Intent.")

        if mode == "basic":
            df = pd.DataFrame(columns=["display_name", "training_phrase"])

            intent_dict = {"display_name": str(obj.display_name)}

            if not obj.training_phrases:
                row = pd.DataFrame.from_dict(
                    intent_dict, orient="index"
                ).transpose()
                df = pd.concat([df, row], ignore_index=True)
            else:
                for tp in obj.training_phrases:
                    parts_list = [part.text for part in tp.parts]
                    intent_dict.update({"training_phrase": "".join(parts_list)})

                    row = pd.DataFrame.from_dict(
                        intent_dict, orient="index"
                    ).transpose()
                    df = pd.concat([df, row], ignore_index=True)

            return df

        elif mode == "advanced":
            df = pd.DataFrame(columns=[
                "name", "display_name", "description", "priority",
                "is_fallback", "labels", "id", "repeat_count",
                "training_phrase_idx", "text", "text_idx",
                "parameter_id", "entity_type", "is_list", "redact",
            ])

            intent_dict = {
                "name": str(obj.name),
                "display_name": str(obj.display_name),
                "description": str(obj.description),
                "priority": int(obj.priority),
                "is_fallback": bool(obj.is_fallback),
            }

            # labels
            intent_dict["labels"] = ",".join([
                key if key == val else f"{key}:{val}"
                for key, val in obj.labels.items()
            ])
            # parameters
            params_dict = {
                str(param.id): {
                    "parameter_id": str(param.id),
                    "entity_type": str(param.entity_type),
                    "is_list": bool(param.is_list),
                    "redact": bool(param.redact),
                }
                for param in obj.parameters
            }
            # training phrases
            if not obj.training_phrases:
                row = pd.DataFrame.from_dict(
                    intent_dict, orient="index"
                ).transpose()
                df = pd.concat([df, row], ignore_index=True)
            else:
                for tp_count, tp in enumerate(obj.training_phrases):
                    intent_dict.update({
                        "id": str(tp.id),
                        "repeat_count": int(tp.repeat_count),
                        "training_phrase_idx": tp_count,
                    })
                    for part_count, part in enumerate(tp.parts):
                        intent_dict.update({
                            "text": part.text,
                            "text_idx": part_count
                        })
                        if part.parameter_id:
                            intent_dict.update(params_dict[part.parameter_id])
                        elif intent_dict.get("entity_type"):
                            # Remove existing parameter_id if exist
                            key_to_remove = [
                                "parameter_id", "entity_type",
                                "is_list", "redact",
                            ]
                            for key in key_to_remove:
                                intent_dict.pop(key)
                        # Add to Dataframe
                        row = pd.DataFrame.from_dict(
                            intent_dict, orient="index"
                        ).transpose()
                        df = pd.concat([df, row], ignore_index=True)
            return df
        else:
            raise ValueError("Mode types: [basic, advanced]")


    @staticmethod
    def modify_training_phrase_df(
        actions: pd.DataFrame, training_phrase_df: pd.DataFrame
    ):
        """
        Update the advanced mode training phrases dataframe to reflect the
        actions provided in the actions dataframe. Pass the returned new
        training phrase dataframe and the original parameters dataframe to the
        bulk_update_intents_from_dataframe functions with update_flag = True in
        order to make these edits. Entities in training phrases not touched
        will be maintained. Entities will not be added in phrases which are
        added or moved.

        Args:
          actions:
            display_name, display_name of the intent to take action on
            phrase, exact string training phrase to take action on
            action, add or delete. To do a move it is an add + a delete action
              of the same phrase
            training_phrase_df, advanced mode training phrase dataframe pulled

        Returns:
          updated_training_phrases_df, training phrase df from advanced mode
            with the edits made to it shown in the actions_taken dataframe
          actions_taken: actions taken based on the actions provided. For
            example we cannot delete a training phrase which does not exit;
            this will be shown.
        """
        mutations = pd.merge(
            training_phrase_df,
            actions,
            on=["display_name", "phrase"],
            how="outer",
        )

        # untouched
        untouched = mutations[mutations["action"].isna()]

        # Adding new phrases
        true_additions = mutations[
            (mutations["action"] == "add") & (mutations["text"].isna())
        ]
        false_additions = mutations[
            (mutations["action"] == "add") & (~mutations["text"].isna())
        ]

        true_additions = true_additions.drop(
            columns=[
                "name",
                "training_phrase",
                "part",
                "text",
                "parameter_id",
                "repeat_count",
                "id",
            ]
        )
        true_additions["name"] = list(set(untouched["name"]))[0]

        next_tp_id = int(untouched["training_phrase"].max() + 1)
        last_tp_id = int(
            untouched["training_phrase"].max() + len(true_additions)
        )

        true_additions["training_phrase"] = list(
            range(next_tp_id, last_tp_id + 1)
        )
        true_additions["part"] = 0
        true_additions["text"] = true_additions["phrase"]
        true_additions["parameter_id"] = ""
        true_additions["repeat_count"] = 1
        true_additions["id"] = ""
        true_additions = true_additions[untouched.columns]

        # Deleting existing phrases
        true_deletions = mutations.copy()[
            (mutations["action"] == "delete") & (~mutations["text"].isna())
        ]
        false_deletions = mutations.copy()[
            (mutations["action"] == "delete") & (mutations["text"].isna())
        ]

        updated_training_phrases_df = untouched.copy()
        updated_training_phrases_df = pd.concat([
            updated_training_phrases_df,
            false_additions
        ])
        updated_training_phrases_df = pd.concat([
            updated_training_phrases_df,
            true_additions
        ])
        updated_training_phrases_df = updated_training_phrases_df.drop(
            columns=["action"]
        )

        actions_taken = pd.DataFrame()
        if true_additions.empty is False:
            true_additions.insert(len(true_additions.columns), "completed", 1)
            true_additions.insert(
                len(true_additions.columns), "category", "true addition"
            )
            true_additions.insert(
                len(true_additions.columns),
                "outcome",
                true_additions.apply(
                    lambda x: "{} added to {}".format( #pylint: disable=C0209
                        x["phrase"], x["display_name"]
                    ),
                    axis=1,
                ),
            )
            actions_taken = pd.concat([actions_taken, true_additions])

        if false_additions.empty is False:
            false_additions.insert(len(false_additions.columns), "completed", 0)
            false_additions.insert(
                len(false_additions.columns), "category", "false addition"
            )
            false_additions.insert(
                len(false_additions.columns),
                "outcome",
                false_additions.apply(
                    lambda x: "{} already in {}".format( #pylint: disable=C0209
                        x["phrase"], x["display_name"]
                    ),
                    axis=1,
                ),
            )
            actions_taken = pd.concat([actions_taken, false_additions])

        if true_deletions.empty is False:
            true_deletions.insert(len(true_deletions.columns), "completed", 1)
            true_deletions.insert(
                len(true_deletions.columns), "category", "true deletion"
            )
            true_deletions.insert(
                len(true_deletions.columns),
                "outcome",
                true_deletions.apply(
                    lambda x: "{} removed from {}".format( #pylint: disable=C0209
                        x["phrase"], x["display_name"]
                    ),
                    axis=1,
                ),
            )
            actions_taken = pd.concat([actions_taken, true_deletions])

        if false_deletions.empty is False:
            false_deletions.insert(len(false_deletions.columns), "completed", 0)
            false_deletions.insert(
                len(false_deletions.columns), "category", "false deletion"
            )
            false_deletions.insert(
                len(false_deletions.columns),
                "outcome",
                false_deletions.apply(
                    lambda x: "{} not found in {}".format( #pylint: disable=C0209
                        x["phrase"], x["display_name"]
                    ),
                    axis=1,
                ),
            )
            actions_taken = pd.concat([actions_taken, false_deletions])

        actionable_intents = list(
            set(actions_taken[actions_taken["completed"] == 1]["display_name"])
        )

        updated_training_phrases_df = updated_training_phrases_df[
            updated_training_phrases_df["display_name"].isin(actionable_intents)
        ]
        return_data = {
            "updated_training_phrases_df": updated_training_phrases_df,
            "actions_taken": actions_taken,
        }

        return return_data

    def get_intents_map(self, agent_id: str = None, reverse: bool = False):
        """Exports Agent Intent Names and UUIDs into a user friendly dict.

        Args:
          agent_id, the formatted CX Agent ID to use
          reverse, (Optional) Boolean flag to swap key:value -> value:key

        Returns:
          intents_map, Dictionary containing Intent UUIDs as keys and
              intent.display_name as values
        """
        if not agent_id:
            agent_id = self.agent_id

        if reverse:
            intents_dict = {
                intent.display_name: intent.name
                for intent in self.list_intents(agent_id)
            }

        else:
            intents_dict = {
                intent.name: intent.display_name
                for intent in self.list_intents(agent_id)
            }

        return intents_dict

    def list_intents(
        self,
        agent_id: str = None,
        language_code: str = None) -> List[types.Intent]:
        """Exports List of all intents in specific CX Agent.

        Args:
          agent_id, the formatted CX Agent ID to use
          language_code: Language code of the intents being uploaded. Ref:
            https://cloud.google.com/dialogflow/cx/docs/reference/language

        Returns:
          intents, List of Intent objects
        """
        if not agent_id:
            agent_id = self.agent_id

        request = types.intent.ListIntentsRequest()

        if language_code:
            request.language_code = language_code

        request.parent = agent_id
        client_options = self._set_region(agent_id)
        client = services.intents.IntentsClient(
            credentials=self.creds, client_options=client_options
        )
        response = client.list_intents(request)

        intents = []
        for page in response.pages:
            for intent in page.intents:
                intents.append(intent)

        return intents

    def get_intent(
        self,
        intent_id: str = None,
        language_code: str = None) -> types.Intent:
        """Get a single Intent object based on specific CX Intent ID.

        Args:
          intent_id, the properly formatted CX Intent ID
          language_code: Language code of the intents being uploaded. Ref:
            https://cloud.google.com/dialogflow/cx/docs/reference/language

        Returns:
          response, a single Intent object
        """
        if not intent_id:
            intent_id = self.intent_id

        request = types.intent.GetIntentRequest()

        if language_code:
            request.language_code = language_code

        request.name = intent_id
        client_options = self._set_region(intent_id)
        client = services.intents.IntentsClient(
            credentials=self.creds, client_options=client_options
        )

        response = client.get_intent(request)

        return response

    def create_intent(
        self,
        agent_id: str,
        obj: types.Intent = None,
        intent_dictionary: dict = None,
        language_code: str = None) -> types.Intent:
        """Creates an Intent from a protobuf or dictionary.

        Args:
          agent_id, the formatted CX Agent ID to use
          obj, (Optional) Intent protobuf of types.Intent
          intent_dictionary, (optional) dictionary of the intent to pass in
            with structure

        example intent dictionary:
           test_intent = {
            "description": "",
            "display_name": "my_intent",
            "is_fallback": False,
            "labels": {},
            "priority": 500000,
            "training_phrases": [
                {
                    "id": "",
                    "parts": [
                        {
                            "text": "hello"
                        },
                        {
                            "text": "all"
                        }
                    ],
                    "repeat_count": 1
                },
                {
                    "id": "",
                    "parts": [
                        {
                            "text": "hi"
                        }
                    ],
                    "repeat_count": 1
                }
            ]
        }

        Returns:
          Intent protobuf object
        """

        if obj and intent_dictionary:
            raise ValueError("cannot provide both obj and intent_dictionary")
        elif obj:
            intent = obj
            intent.name = ""
        elif intent_dictionary:
            intent = types.intent.Intent.from_json(
                json.dumps(intent_dictionary)
            )
        else:
            raise ValueError("must provide either obj or intent_dictionary")

        request = types.intent.CreateIntentRequest()

        if language_code:
            request.language_code = language_code

        request.parent = agent_id
        request.intent = intent

        client_options = self._set_region(agent_id)
        client = services.intents.IntentsClient(
            client_options=client_options, credentials=self.creds
        )

        response = client.create_intent(request)

        return response

    def update_intent(
        self,
        intent_id: str = None,
        obj: types.Intent = None,
        language_code: str = None,
        **kwargs) -> types.Intent:
        """Updates a single Intent object based on provided args.
        Args:
          intent_id, the destination Intent ID. Must be formatted properly
            for Intent IDs in CX.
          obj, The CX Intent object in proper format. This can also be
            extracted by using the get_intent() method.
          language_code: Language code of the intents being uploaded. Ref:
            https://cloud.google.com/dialogflow/cx/docs/reference/language
        """
        if obj:
            intent = obj
            intent.name = intent_id

        else:
            if not intent_id:
                intent_id = self.intent_id
            intent = self.get_intent(intent_id)

        # set intent attributes from kwargs
        for key, value in kwargs.items():
            setattr(intent, key, value)
        paths = kwargs.keys()
        mask = field_mask_pb2.FieldMask(paths=paths)

        client_options = self._set_region(intent_id)
        client = services.intents.IntentsClient(
            client_options=client_options, credentials=self.creds
        )

        request = types.intent.UpdateIntentRequest()

        request.intent = intent
        request.update_mask = mask

        if language_code:
            request.language_code = language_code

        response = client.update_intent(request)

        return response

    def delete_intent(self, intent_id: str, obj: types.Intent = None) -> None:
        """Deletes an intent by Intent ID.

        Args:
          intent_id, intent to delete
        """
        if obj:
            intent_id = obj.name
        else:
            client_options = self._set_region(intent_id)
            client = services.intents.IntentsClient(
                client_options=client_options, credentials=self.creds
            )
            client.delete_intent(name=intent_id)

    def bulk_intent_to_df(
        self,
        agent_id: str = None,
        mode: str = "basic",
        intent_subset: List[str] = None,
        transpose: bool = False,
        language_code: str = None) -> pd.DataFrame:
        """Extracts all Intents and Training Phrases into a Pandas DataFrame.

        Args:
          agent_id (str):
            agent to pull list of intents
          mode (str):
            basic returns display name and training phrase as plain text.
            advanced returns training phrases broken out by parts
            with their parameters included.
          intent_subset (List[str]):
            A subset of intents to extract the intents from.
          transpose (bool):
            Return the transposed DataFrame. If this flag passed as True,
            mode won't affect the result and the result would be like basic.
          language_code (str):
            Language code of the intents being uploaded. Ref:
            https://cloud.google.com/dialogflow/cx/docs/reference/language
        """

        if not agent_id:
            agent_id = self.agent_id

        if transpose:
            _, intents_dict = self.intents_to_df_cosine_prep(agent_id)
            transposed_df = pd.DataFrame.from_dict(
                intents_dict, "index"
            ).transpose()
            if intent_subset:
                transposed_df = transposed_df[intent_subset]

            return transposed_df

        if mode not in ["basic", "advanced"]:
            raise ValueError("Mode types: [basic, advanced]")

        main_df = pd.DataFrame()
        intents = self.list_intents(agent_id, language_code=language_code)

        for obj in intents:
            if (intent_subset) and (obj.display_name not in intent_subset):
                continue
            intent_df = self.intent_proto_to_dataframe(obj, mode=mode)
            main_df = pd.concat([main_df, intent_df], ignore_index=True)

        return main_df

    def intents_to_df_cosine_prep(
        self,
        agent_id: str = None) -> Tuple[pd.DataFrame, Dict[str,str]]:
        """Exports a dataframe and defaultdict of Intents for use with Cosine
        Similarity tools.

        Args:
          agent_id, agent to pull list of intents from

        Returns:
          df, a Pandas Dataframe of Intents and TPs
          intent_dict, a Defaultdict(List) that is prepped to feed to the
            Cosine similarity tool (offline)
        """
        if not agent_id:
            agent_id = self.agent_id

        intent_dict = defaultdict(list)
        intents = self.list_intents(agent_id)

        for intent in intents:  # pylint: disable=R1702
            if intent.display_name == "Default Negative Intent":
                pass
            else:
                if "training_phrases" in intent:
                    for training_phrase in intent.training_phrases:
                        text_list = []
                        if len(training_phrase.parts) > 1:
                            for item in training_phrase.parts:
                                text_list.append(item.text)
                            intent_dict[intent.display_name].append(
                                "".join(text_list))
                        else:
                            intent_dict[intent.display_name].append(
                                training_phrase.parts[0].text
                            )
                else:
                    intent_dict[intent.display_name].append("")

        dataframe = pd.DataFrame.from_dict(
            intent_dict, orient="index").transpose()
        dataframe = dataframe.stack().to_frame().reset_index(level=1)
        dataframe = dataframe.rename(
            columns={"level_1": "display_name",
            0: "training_phrase"}).reset_index(drop=True)
        dataframe = dataframe.sort_values(["display_name", "training_phrase"])

        return dataframe, intent_dict
