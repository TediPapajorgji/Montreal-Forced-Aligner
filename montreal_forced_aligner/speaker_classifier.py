"""
Speaker classification
======================


"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, List, Optional

from praatio import textgrid
from sqlalchemy.orm import joinedload, selectinload

from montreal_forced_aligner.abc import FileExporterMixin, TopLevelMfaWorker
from montreal_forced_aligner.alignment.multiprocessing import construct_output_path
from montreal_forced_aligner.corpus.ivector_corpus import IvectorCorpusMixin
from montreal_forced_aligner.data import TextFileType
from montreal_forced_aligner.db import File, SoundFile, SpeakerOrdering, TextFile
from montreal_forced_aligner.exceptions import KaldiProcessingError
from montreal_forced_aligner.helper import load_configuration, load_scp, mfa_open
from montreal_forced_aligner.models import IvectorExtractorModel
from montreal_forced_aligner.utils import log_kaldi_errors

if TYPE_CHECKING:
    from argparse import Namespace

    from .abc import MetaDict
__all__ = ["SpeakerClassifier"]


class SpeakerClassifier(
    IvectorCorpusMixin, TopLevelMfaWorker, FileExporterMixin
):  # pragma: no cover
    """
    Class for performing speaker classification, not currently very functional, but
    is planned to be expanded in the future

    Parameters
    ----------
    ivector_extractor_path : str
        Path to ivector extractor model
    expected_num_speakers: int, optional
        Number of speakers in the corpus, if known
    cluster: bool, optional
        Flag for whether speakers should be clustered instead of classified
    """

    def __init__(
        self,
        ivector_extractor_path: str,
        expected_num_speakers: int = 0,
        cluster: bool = True,
        **kwargs,
    ):
        self.ivector_extractor = IvectorExtractorModel(ivector_extractor_path)
        kwargs.update(self.ivector_extractor.parameters)
        super().__init__(**kwargs)
        self.classifier = None
        self.speaker_labels = {}
        self.ivectors = {}
        self.expected_num_speakers = expected_num_speakers
        self.cluster = cluster

    @classmethod
    def parse_parameters(
        cls,
        config_path: Optional[str] = None,
        args: Optional[Namespace] = None,
        unknown_args: Optional[List[str]] = None,
    ) -> MetaDict:
        """
        Parse parameters for speaker classification from a config path or command-line arguments

        Parameters
        ----------
        config_path: str
            Config path
        args: :class:`~argparse.Namespace`
            Command-line arguments from argparse
        unknown_args: list[str], optional
            Extra command-line arguments

        Returns
        -------
        dict[str, Any]
            Configuration parameters
        """
        global_params = {}
        if config_path and os.path.exists(config_path):
            data = load_configuration(config_path)
            for k, v in data.items():
                if k == "features":
                    if "type" in v:
                        v["feature_type"] = v["type"]
                        del v["type"]
                    global_params.update(v)
                else:
                    if v is None and k in cls.nullable_fields:
                        v = []
                    global_params[k] = v
        global_params.update(cls.parse_args(args, unknown_args))
        return global_params

    @property
    def workflow_identifier(self) -> str:
        """Speaker classification identifier"""
        return "speaker_classification"

    @property
    def ie_path(self) -> str:
        """Path for the ivector extractor model file"""
        return os.path.join(self.working_directory, "final.ie")

    @property
    def model_path(self) -> str:
        """Path for the acoustic model file"""
        return os.path.join(self.working_directory, "final.mdl")

    @property
    def dubm_path(self) -> str:
        """Path for the DUBM model"""
        return os.path.join(self.working_directory, "final.dubm")

    def setup(self) -> None:
        """
        Sets up the corpus and speaker classifier

        Raises
        ------
        :class:`~montreal_forced_aligner.exceptions.KaldiProcessingError`
            If there were any errors in running Kaldi binaries
        """

        self.check_previous_run()
        done_path = os.path.join(self.working_directory, "done")
        if os.path.exists(done_path):
            self.log_info("Classification already done, skipping initialization.")
            return
        log_dir = os.path.join(self.working_directory, "log")
        os.makedirs(log_dir, exist_ok=True)
        try:
            self.load_corpus()
            self.ivector_extractor.export_model(self.working_directory)
            self.extract_ivectors()
        except Exception as e:
            if isinstance(e, KaldiProcessingError):
                import logging

                logger = logging.getLogger(self.identifier)
                log_kaldi_errors(e.error_logs, logger)
                e.update_log_file(logger)
            raise

    def load_ivectors(self) -> None:
        """
        Load ivectors from the temporary directory
        """
        self.ivectors = {}
        for ivectors_args in self.extract_ivectors_arguments():
            ivec = load_scp(ivectors_args.ivectors_path)
            for utt, ivector in ivec.items():
                ivector = [float(x) for x in ivector]
                self.ivectors[utt] = ivector

    def cluster_utterances(self) -> None:
        """
        Cluster utterances based on their ivectors
        """
        self.log_error(
            "Speaker diarization functionality is currently under construction and not working in the current version."
        )
        raise NotImplementedError(
            "Speaker diarization functionality is currently under construction and not working in the current version."
        )

    def export_files(self, output_directory: str) -> None:
        """
        Export files with their new speaker labels

        Parameters
        ----------
        output_directory: str
            Output directory to save files
        """
        if not self.overwrite and os.path.exists(output_directory):
            output_directory = os.path.join(self.working_directory, "transcriptions")
        os.makedirs(output_directory, exist_ok=True)

        with self.session() as session:
            files = session.query(File).options(
                selectinload(File.utterances),
                selectinload(File.speakers).selectinload(SpeakerOrdering.speaker),
                joinedload(File.sound_file, innerjoin=True).load_only(SoundFile.duration),
                joinedload(File.text_file, innerjoin=True).load_only(TextFile.file_type),
            )
            for file in files:
                utterance_count = len(file.utterances)
                duration = file.sound_file.duration

                if utterance_count == 0:
                    self.log_debug(f"Could not find any utterances for {file.name}")
                output_path = construct_output_path(
                    file.name,
                    file.relative_path,
                    self.output_directory,
                    output_format=file.text_file.file_type,
                )
                data = file.construct_transcription_tiers()
                if file.text_file.file_type == TextFileType.LAB:
                    for intervals in data.values():
                        with mfa_open(output_path, "w") as f:
                            f.write(intervals[0].label)
                else:

                    tg = textgrid.Textgrid()
                    tg.minTimestamp = 0
                    tg.maxTimestamp = duration
                    for speaker in file.speakers:
                        speaker = speaker.speaker.name
                        intervals = data[speaker]
                        tier = textgrid.IntervalTier(
                            speaker, [x.to_tg_interval() for x in intervals], minT=0, maxT=duration
                        )

                        tg.addTier(tier)
                    tg.save(output_path, includeBlankSpaces=True, format=file.text_file.file_type)
