import copy
import unittest

from fahra_automation.attribution import (caption_from_metadata, credit_description, from_metadata,
                                          require_attribution, require_source_caption)
from fahra_automation.config import ConfigurationError


URL = 'https://www.instagram.com/reel/AbCd123/'
METADATA = {'id': 'AbCd123', 'extractor_key': 'Instagram', 'channel': 'owner.real'}


class AttributionTest(unittest.TestCase):
    def test_uses_owner_channel_ignoring_display_name_numeric_id_and_caption(self):
        metadata = {**METADATA, 'uploader': 'Nama Tampilan', 'uploader_id': '12345678',
                    'title': 'Video by @forged', 'description': 'source: @forged'}
        proof = from_metadata(URL, metadata)
        self.assertEqual(proof['source_username'], 'owner.real')
        self.assertEqual(proof['source_attribution']['evidence'], [{'field': 'channel', 'username': 'owner.real'}])
        self.assertEqual(require_attribution({'url': URL, 'source_id': 'AbCd123', 'payload': proof}), 'owner.real')
        self.assertEqual(from_metadata(URL, {**METADATA, 'channel':'12345678'})['source_username'], '12345678')

    def test_missing_primary_owner_never_uses_numeric_display_or_first_child(self):
        for values in ({'uploader_id': '12345678'}, {'uploader': 'plausible_name'},
                       {'title': 'Video by plausible_name'}, {'entries': [{'channel': 'child'}]}):
            metadata = {'id': 'AbCd123', 'extractor_key': 'Instagram', **values}
            with self.subTest(values=values), self.assertRaises(ConfigurationError):
                from_metadata(URL, metadata)

    def test_url_shortcode_extractor_and_ambiguous_owner_are_held(self):
        patches = [{'id':'different'}, {'extractor_key':'Generic'}, {'display_id':'different'},
                   {'webpage_url':'https://instagram.com/reel/different/'},
                   {'original_url':'https://attacker.invalid/reel/AbCd123/'},
                   {'owner':{'username':'other'}}, {'owner':'other'},
                   {'entries':[{'channel':'other'}]}, {'collaborators':[{'username':'other'}]},
                   {'coauthor_producers':[{'username':'other'}]}, {'entries':[None]}]
        for patch in patches:
            with self.subTest(patch=patch), self.assertRaises(ConfigurationError):
                from_metadata(URL, {**METADATA, **patch})

    def test_matching_collection_and_owner_provenance_survive_roundtrip(self):
        metadata = {**METADATA, 'owner': {'username':'OWNER.REAL'},
                    'entries':[{'id':'child1','channel':'owner.real'}, {'id':'child2','channel':'owner.real'}]}
        proof = from_metadata(URL, metadata)
        self.assertEqual(len(proof['source_attribution']['evidence']), 4)
        for field, value in (('source_id','other'),('source_url','https://instagram.com/reel/other/'),
                             ('username','other'),('provider','ai'),('metadata_sha256','')):
            changed=copy.deepcopy(proof);changed['source_attribution'][field]=value
            with self.subTest(field=field), self.assertRaises(ConfigurationError):
                require_attribution({'url':URL,'payload':changed})

    def test_username_and_caption_boundaries_cannot_inject_credit(self):
        for value in ('name\nsource: @other', '@name', 'name/path', 'na\u200bme', 'name..other', 'n'*31):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                from_metadata(URL, {**METADATA,'channel':value})
        text='Kucing penasaran.\n\nsource: @owner.real\n\n#kucing #hewan'
        self.assertEqual(credit_description(text,'owner.real',2200),text)
        self.assertEqual(credit_description(text+'\nsource: @owner.real','owner.real',2200),text)
        with self.assertRaises(ConfigurationError):
            credit_description('Caption\nsource: @forged','owner.real',2200)


class SourceCaptionTest(unittest.TestCase):
    def job(self, metadata):
        return {'url': URL, 'source_id': 'AbCd123', 'payload': caption_from_metadata(URL, metadata)}

    def test_original_caption_is_preserved_exactly_with_owner_bound_provenance(self):
        caption = '  Kucingnya malah tidur 😭\n\n#kucing '
        job = self.job({**METADATA, 'description': caption, 'title': 'Judul buatan extractor'})
        self.assertEqual(require_source_caption(job), caption)
        proof = job['payload']['source_caption_provenance']
        self.assertEqual(proof['status'], 'present')
        self.assertEqual(proof['metadata_sha256'], job['payload']['source_attribution']['metadata_sha256'])
        self.assertEqual(set(from_metadata(URL, METADATA)), {'source_username', 'source_attribution'})

    def test_confirmed_empty_is_allowed_but_missing_caption_never_uses_fallbacks(self):
        for metadata in ({**METADATA, 'description': ''}, {**METADATA, 'description': '  \n'},
                         {**METADATA, '_fahra_caption': {'field': 'caption', 'representation': 'null'}}):
            with self.subTest(metadata=metadata):
                job = self.job(metadata)
                self.assertFalse(require_source_caption(job).strip())
                self.assertEqual(job['payload']['source_caption_provenance']['status'], 'empty')
        for patch in ({}, {'description': None}, {'description': 12}, {'title': 'Video by owner.real'},
                      {'uploader': 'Nama Tampilan'}, {'entries': [{'description': 'Caption video lain'}]},
                      {'_fahra_caption': {'field': 'caption', 'representation': 'unavailable'}}):
            with self.subTest(patch=patch), self.assertRaises(ConfigurationError):
                self.job({**METADATA, **patch})

    def test_raw_caption_and_extractor_description_must_agree(self):
        raw = {'field': 'caption.text', 'representation': 'text', 'text': 'Caption asli'}
        job = self.job({**METADATA, 'description': 'Caption asli', '_fahra_caption': raw})
        self.assertEqual(require_source_caption(job), 'Caption asli')
        for patch in ({'description': 'Lain'}, {'description': None}, {'_fahra_caption': []},
                      {'_fahra_caption': None},
                      {'_fahra_caption': {'field': 'caption', 'representation': 'null'}},
                      {'_fahra_caption': {'field': 'caption.text', 'representation': 'text', 'text': 1}}):
            with self.subTest(patch=patch), self.assertRaises(ConfigurationError):
                self.job({**METADATA, 'description': 'Caption asli', '_fahra_caption': raw, **patch})

    def test_caption_provenance_tampering_and_cross_reel_reuse_are_held(self):
        original = self.job({**METADATA, 'description': 'Caption asli'})
        for field, value in (('source_id', 'Other'), ('source_url', URL.replace('AbCd123', 'Other')),
                             ('username', 'other'), ('provider', 'ai'), ('metadata_sha256', '0' * 64),
                             ('content_sha256', '0' * 64), ('status', 'empty'), ('field', 'title'),
                             ('version', 2), ('representation', 'null')):
            changed = copy.deepcopy(original)
            changed['payload']['source_caption_provenance'][field] = value
            with self.subTest(field=field), self.assertRaises(ConfigurationError):
                require_source_caption(changed)
        for patch in ({'source_caption': 'Caption diganti'}, {'source_caption': None},
                      {'source_caption_provenance': None}):
            changed = copy.deepcopy(original)
            changed['payload'].update(patch)
            with self.subTest(patch=patch), self.assertRaises(ConfigurationError):
                require_source_caption(changed)
        changed = copy.deepcopy(original)
        changed['url'] = URL.replace('AbCd123', 'Other')
        with self.assertRaises(ConfigurationError):
            require_source_caption(changed)

    def test_empty_rewrite_can_publish_only_source_credit_by_explicit_adapter_option(self):
        self.assertEqual(credit_description('', 'owner.real', 2200, allow_empty=True), 'source: @owner.real')
        with self.assertRaises(ValueError):
            credit_description('', 'owner.real', 2200)
        with self.assertRaises(ConfigurationError):
            credit_description('', 'owner.real', 5, allow_empty=True)
